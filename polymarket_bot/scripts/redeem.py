"""Claim every settled position.

    python -m polymarket_bot.scripts.redeem
    python -m polymarket_bot.scripts.redeem --json

Redeeming is the one money-moving action with no downside: a settled position
either pays out or it does not, and claiming it cannot lose you anything. That
is why this command has no --execute flag and no typed confirmation, unlike
`place_test_order` and `monitor`.

What it does NOT do is recover a loss. A position that resolved against you is
still listed as redeemable - the exchange always lists it - and claiming it
pays $0.00 and clears it from the list. The report says which is which.

Exit code 0 means the call succeeded (including "there was nothing to claim");
1 means it failed or some markets could not be redeemed.
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


def _run(args: argparse.Namespace) -> int:
    response = service.redeem()
    if args.json:
        return print_response(response, as_json=True)
    emit(str(response.get("text") or ""))
    return 0 if response.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "redeem",
        "Claim every settled position. Losers pay $0.00 and are only cleared from the list.",
        epilog=__doc__,
    )
    add_json_flag(parser)
    return dispatch(_run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
