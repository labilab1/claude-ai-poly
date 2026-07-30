"""Pluggable notification sinks.

Core modules never print. They emit events through a Notifier so the same
logic can drive a terminal today and a Telegram chat later — the Telegram
layer only has to implement `send`.
"""

from __future__ import annotations

import sys
from typing import Literal, Protocol, runtime_checkable

Level = Literal["debug", "info", "trade", "alert", "error"]

# ASCII only, deliberately. This console is cp1255 (Hebrew codepage) and any
# non-encodable character raises UnicodeEncodeError mid-print — which would
# take down a trade path for the sake of decoration.
_PREFIX: dict[str, str] = {
    "debug": "  .",
    "info": "  -",
    "trade": " $$",
    "alert": " !!",
    "error": "ERR",
}


@runtime_checkable
class Notifier(Protocol):
    def send(self, message: str, *, level: Level = "info") -> None: ...


class NullNotifier:
    """Swallows everything. Default for library use and tests."""

    def send(self, message: str, *, level: Level = "info") -> None:  # noqa: D102
        return None


class ConsoleNotifier:
    """Prints to stdout. Used by the CLI scripts."""

    def __init__(self, min_level: Level = "info") -> None:
        order: list[Level] = ["debug", "info", "trade", "alert", "error"]
        self._allowed = set(order[order.index(min_level) :])

    def send(self, message: str, *, level: Level = "info") -> None:  # noqa: D102
        if level not in self._allowed:
            return
        line = f"{_PREFIX.get(level, '  -')} {message}"
        try:
            print(line)
        except UnicodeEncodeError:
            # Market titles can carry characters this console cannot encode.
            # Degrade the text rather than lose the message (or crash a trade).
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            print(line.encode(encoding, errors="replace").decode(encoding, errors="replace"))


class CollectingNotifier:
    """Buffers messages in memory.

    Useful for request/response transports (a Telegram command handler can
    collect everything a call emitted and send one combined reply).
    """

    def __init__(self) -> None:
        self.messages: list[tuple[Level, str]] = []

    def send(self, message: str, *, level: Level = "info") -> None:  # noqa: D102
        self.messages.append((level, message))

    def drain(self) -> list[tuple[Level, str]]:
        out = list(self.messages)
        self.messages.clear()
        return out

    def text(self) -> str:
        return "\n".join(f"{_PREFIX.get(lvl, '•')} {msg}" for lvl, msg in self.messages)


class MultiNotifier:
    """Fans out to several notifiers; one failing sink never breaks the others."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = [n for n in notifiers if n is not None]

    def send(self, message: str, *, level: Level = "info") -> None:  # noqa: D102
        for notifier in self._notifiers:
            try:
                notifier.send(message, level=level)
            except Exception:  # a broken sink must not abort trading logic
                continue
