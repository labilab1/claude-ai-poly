"""Cancel every resting order.

    python -m polymarket_bot.scripts.cancel
    python -m polymarket_bot.scripts.cancel --json

Cancelling costs nothing and cannot lose money - it removes orders that have
not filled - so there is no --execute flag or typed confirmation here.

The consequence worth knowing before you run it: a take-profit left RESTING on
the exchange as a SELL limit order is a resting order. This cancels that too,
and a resting take-profit is the only exit that survives the bot being offline
(see README, "Polymarket has no native stop orders"). After cancelling, exits
depend entirely on `monitor` running.

Exit code 0 means everything was cancelled; 1 means the call failed or some
orders are STILL RESTING on the book - a partial cancel is not success, and
the report names the survivors.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.scripts._common import (
    add_json_flag,
    build_parser,
    dispatch,
    emit,
    print_response,
)

_WARNING = (
    "Note: this also cancels any take-profit resting on the book as a limit\n"
    "order - the only exit that works while the bot is offline."
)


def _run(args: argparse.Namespace) -> int:
    if args.json:
        return print_response(service.cancel_orders(), as_json=True)

    # Printed before the call, not after: if the call hangs or the process is
    # interrupted, the user has still seen what this command gives up.
    emit(_WARNING)
    emit("")
    response = service.cancel_orders()
    emit(str(response.get("text") or ""))
    return 0 if response.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "cancel",
        "Cancel every resting order, including any take-profit resting as a limit order.",
        epilog=__doc__,
    )
    add_json_flag(parser)
    return dispatch(_run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
