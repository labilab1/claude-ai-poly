"""Watch open positions and enforce the stored exit rules.

    # One dry-run pass and exit (safe, sends nothing):
    python -m polymarket_bot.scripts.monitor --once

    # Keep sweeping until Ctrl-C, still dry-run:
    python -m polymarket_bot.scripts.monitor
    python -m polymarket_bot.scripts.monitor --interval 30

    # Same, but actually send the sells a triggered rule asks for:
    python -m polymarket_bot.scripts.monitor --execute
    python -m polymarket_bot.scripts.monitor --once --execute --yes

DRY RUN IS THE DEFAULT AND IS FORCED. This command sets the dry-run flag from
its own arguments rather than inheriting it, so a `.env` with
POLYMARKET_MONITOR_DRY_RUN=false cannot make a plain `monitor` run live: only
--execute does that, and only after a typed confirmation (or --yes).

Continuous mode runs ONE `Monitor` and hands it the loop (`Monitor.run_forever`).
That matters for more than tidiness: the monitor announces a halt, a resume and
a retired rule *on transition*, and it keeps one authenticated client for the
whole session. Rebuilding a Monitor and a SecureClient every pass - which this
script used to do - re-authenticates constantly and makes every pass look like
the first one, so a halted monitor re-announces the halt every single interval.
Output in continuous mode is therefore the monitor's own event stream rather
than a report block per pass. `--once` still goes through `service.monitor_once`
and prints the full sweep report.

Polymarket has no native stop-loss. A stop-loss or trailing stop stored by
`scripts.rules` exists only inside this process - while this is not running,
those rules protect nothing. A take-profit can alternatively be left resting on
the exchange as a SELL limit order, which does survive the bot being offline.

With TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID both set, continuous mode also
pushes trade/alert/error events to that chat, so a rule that fires while you
are away from the terminal still reaches your phone. Routine per-pass chatter
stays on the console. This is a direct HTTP call to Telegram - the separate
`scripts.telegram_bot` process does not need to be running.
"""

from __future__ import annotations

import argparse
import json
import os

from polymarket_bot import service
from polymarket_bot.client import get_client
from polymarket_bot.config import Settings, load_settings
from polymarket_bot.notify import ConsoleNotifier, Level, MultiNotifier, Notifier
from polymarket_bot.rules import RuleStore
from polymarket_bot.scripts._common import (
    add_json_flag,
    build_parser,
    confirm_execution,
    dispatch,
    dump_json,
    emit,
    with_disclaimer,
)


class _JsonNotifier:
    """One JSON object per line, so a `--json` run stays machine-readable.

    Continuous mode has no single response to dump - the monitor emits events as
    it goes - so the stream is JSONL: `{"level": ..., "message": ...}` per line.
    """

    def send(self, message: str, *, level: Level = "info") -> None:
        emit(json.dumps({"level": level, "message": message}))


def _build_notifier(settings: Settings, *, as_json: bool) -> Notifier:
    """Pick the sink for continuous mode.

    With a Telegram token AND a chat id configured, events fan out to both the
    console and the chat, so a stop-loss that fires while nobody is watching
    the terminal still reaches a phone. The Telegram sink filters to
    trade/alert/error on its own - the console keeps the full debug stream.

    `--json` is excluded on purpose: that mode exists for scheduled or piped
    runs whose output is being parsed, and those are not the audience for a
    push notification.
    """
    if as_json:
        return _JsonNotifier()
    console = ConsoleNotifier(min_level="debug")
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return console
    # Imported here so an unconfigured monitor never pays for the import, and
    # so a broken Telegram layer cannot stop a console-only run from starting.
    from polymarket_bot.telegram.api import TelegramAPI
    from polymarket_bot.telegram.notifier import TelegramNotifier

    return MultiNotifier(
        console,
        TelegramNotifier(TelegramAPI(settings.telegram_bot_token), settings.telegram_chat_id),
    )


def _apply_mode(execute: bool, interval: float | None) -> None:
    """Pin dry-run (and the interval) for this process before settings are read.

    `load_settings()` reads the environment on every call, so setting it here
    decides the mode for every service call that follows - including the ones
    made deep inside Monitor. Writing dry-run unconditionally is the point:
    absent --execute the run is dry even if .env says otherwise. The interval
    goes through the same channel because `run_forever` paces itself from
    Settings; whole seconds only, which is what the setting has always been.
    """
    os.environ["POLYMARKET_MONITOR_DRY_RUN"] = "false" if execute else "true"
    if interval is not None:
        os.environ["POLYMARKET_MONITOR_INTERVAL_SECONDS"] = str(max(1, int(interval)))


def _once(args: argparse.Namespace) -> int:
    """A single sweep through the service facade."""
    response = service.monitor_once()

    if args.json:
        dump_json(response)
        return 0 if response.get("ok") else 1

    emit(str(response.get("text") or ""))
    if not response.get("ok"):
        emit("")
        emit(f"Sweep failed: {response.get('error')}")
        return 1
    return 0


def _forever(args: argparse.Namespace) -> int:
    """Continuous mode: one Monitor, one client, its own loop and backoff."""
    # Imported here so a broken monitor module still leaves --once with a clean
    # error from the service layer instead of an import traceback.
    from polymarket_bot.monitor import Monitor

    settings = load_settings()
    notifier = _build_notifier(settings, as_json=args.json)

    monitor = None
    client = get_client()
    try:
        monitor = Monitor(client, settings, notifier=notifier, store=RuleStore(settings))
        # run_forever owns the sleeping, the exponential backoff on failed
        # sweeps and the Ctrl-C handling. Duplicating any of it here is how the
        # two loops drift apart.
        monitor.run_forever(max_iterations=args.max_passes)
    finally:
        try:
            client.close()
        except Exception:
            pass

    # Non-zero if the last sweep never got live state - a scheduled run should
    # be able to tell "watched nothing for an hour" from "watched and saw calm".
    return 1 if getattr(monitor, "_last_run_failed", False) else 0


def _run(args: argparse.Namespace) -> int:
    _apply_mode(args.execute, args.interval)
    settings = load_settings()
    interval = float(settings.monitor_interval_seconds)

    if args.execute and not confirm_execution(
        "LIVE MODE: a triggered rule will send a REAL sell order against real funds.\n"
        f"Execution halts if realized losses pass {settings.daily_loss_limit_usdc:.2f} USDC in 24h. "
        "The per-order cap does not apply to sells - it bounds capital put at risk, and an exit "
        "removes risk.",
        assume_yes=args.yes,
    ):
        return 1

    if not args.json:
        mode = (
            "LIVE - triggered rules will be executed"
            if args.execute
            else "DRY RUN - evaluates only, sends nothing"
        )
        pushes = bool(settings.telegram_bot_token and settings.telegram_chat_id) and not args.once
        header = [
            "MONITOR",
            f"  Mode     : {mode}",
            f"  Interval : {'single pass' if args.once else f'{interval:g}s between passes (Ctrl-C to stop)'}",
            f"  Alerts   : {'console + Telegram' if pushes else 'console only'}",
            "  Note     : stop-loss and trailing rules are enforced only while this runs.",
        ]
        emit(with_disclaimer("\n".join(header)))
        emit("")

    return _once(args) if args.once else _forever(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "monitor",
        "Evaluate stored exit rules against live prices. Dry-run unless --execute.",
        epilog=__doc__,
    )
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Leave dry-run: actually send the sell orders triggered rules ask for.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the typed confirmation for --execute (the caller has already approved live trading).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        help="Seconds between passes, whole seconds (default: POLYMARKET_MONITOR_INTERVAL_SECONDS).",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        help="Stop after this many passes instead of running until Ctrl-C.",
    )
    add_json_flag(parser)

    args = parser.parse_args(argv)
    if args.interval is not None and args.interval < 1:
        parser.error("--interval must be at least 1 second.")
    if args.max_passes is not None and args.max_passes < 1:
        parser.error("--max-passes must be at least 1.")
    return dispatch(_run, args)


if __name__ == "__main__":
    raise SystemExit(main())
