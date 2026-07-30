"""Shared plumbing for the CLI scripts.

The scripts are deliberately thin. Each one turns argv into a single `service`
call and renders the dict that comes back; anything that *decides* something
lives in `service.py` and the core modules, so a Telegram bot driving the same
functions behaves identically to the terminal.

What lives here is presentation only: argparse boilerplate, an ASCII table that
lines up, money formatting, and the exit-code convention every script keeps:

    0 - the call succeeded
    1 - it did not (bad input, refused order, API failure, Ctrl-C)

Output is plain ASCII on purpose. The same strings go to a Windows console, a
redirected pipe and a chat window, and none of them should mangle it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from polymarket_bot.advisor import DISCLAIMER


def harden_streams() -> None:
    """Never let one stray glyph kill a report.

    Core text is ASCII, but advisor briefings carry a few typographic
    characters, and a redirected stdout on Windows falls back to the ANSI code
    page. Replacing the unencodable ones beats a UnicodeEncodeError traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


harden_streams()


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def emit(text: str = "") -> None:
    print(text)


def clip(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: max(width - 3, 0)] + "..."


def money(value: float) -> str:
    return f"${value:,.2f}"


def signed(value: float) -> str:
    # Explicit sign on both sides: "-$0.30" reads faster than "$-0.30".
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def price(value: float) -> str:
    return f"{value:.4f}"


def with_disclaimer(text: str) -> str:
    """Append the advisor disclaimer unless the service text already carries it."""
    if DISCLAIMER in text:
        return text
    return f"{text}\n\n! {DISCLAIMER}"


@dataclass(frozen=True)
class Column:
    header: str
    right: bool = False
    max_width: int | None = None


def render_table(
    columns: Sequence[Column],
    rows: Sequence[Sequence[Any]],
    *,
    indent: str = "  ",
) -> list[str]:
    """A colourless fixed-width table. Columns line up; nothing wraps."""
    body = [
        [clip(str(cell), col.max_width) if col.max_width else str(cell) for cell, col in zip(row, columns)]
        for row in rows
    ]

    widths: list[int] = []
    for index, column in enumerate(columns):
        widest = len(column.header)
        for row in body:
            widest = max(widest, len(row[index]))
        widths.append(widest)

    def line(cells: Sequence[str]) -> str:
        padded = [
            cell.rjust(width) if column.right else cell.ljust(width)
            for cell, width, column in zip(cells, widths, columns)
        ]
        # rstrip so a left-aligned final column leaves no trailing whitespace.
        return (indent + "  ".join(padded)).rstrip()

    rule = indent + "-" * (sum(widths) + 2 * (len(widths) - 1))
    return [line([c.header for c in columns]), rule, *(line(row) for row in body)]


# --------------------------------------------------------------------------
# argparse / dispatch
# --------------------------------------------------------------------------
def build_parser(prog: str, description: str, epilog: str | None = None) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=f"python -m polymarket_bot.scripts.{prog}",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw service response as JSON instead of a report.",
    )


def dump_json(payload: Any) -> None:
    emit(json.dumps(payload, indent=2, default=str))


def print_response(response: dict, *, as_json: bool = False, disclaimer: bool = False) -> int:
    """Render one service response and turn its `ok` flag into an exit code."""
    if as_json:
        dump_json(response)
        return 0 if response.get("ok") else 1
    text = str(response.get("text") or "")
    emit(with_disclaimer(text) if disclaimer else text)
    return 0 if response.get("ok") else 1


def dispatch(handler: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """Run a script body, converting anything unexpected into exit code 1.

    Service calls already swallow their own failures, so what reaches here is a
    missing .env, an unreachable API at client construction, or Ctrl-C.
    """
    try:
        return handler(args)
    except KeyboardInterrupt:
        emit("\nInterrupted - nothing further was sent.")
        return 1
    except Exception as exc:
        emit(f"ERROR: {type(exc).__name__}: {exc}")
        return 1


def confirm_execution(summary: str, *, assume_yes: bool, keyword: str = "EXECUTE") -> bool:
    """Interactive typed confirmation for anything that spends real money.

    `--yes` skips the prompt for callers that have already confirmed the exact
    action elsewhere. A non-interactive stdin is treated as "no": never let a
    piped or scheduled run fall through into a live order by accident.
    """
    if assume_yes:
        return True
    emit("")
    emit(summary)
    try:
        answer = input(f"Type {keyword} to proceed with REAL money (anything else cancels): ")
    except EOFError:
        emit("No terminal to confirm on - cancelled, nothing sent.")
        return False
    if answer.strip() == keyword:
        return True
    emit("Cancelled - nothing sent.")
    return False
