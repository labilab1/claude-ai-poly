"""Scan for binary markets whose YES and NO asks sum to under $1.

    python -m polymarket_bot.scripts.arbitrage
    python -m polymarket_bot.scripts.arbitrage --scan 200 --books 60
    python -m polymarket_bot.scripts.arbitrage --fee 0.05
    python -m polymarket_bot.scripts.arbitrage --json

A YES share and a NO share of the same market always redeem for exactly $1.00
together: one pays $1, the other pays $0, whichever way it resolves. So buying
one of each for less than $1.00 is profit that does not depend on the outcome.
That makes this the one edge in this project that needs no forecast - it is
arithmetic on two prices, not a view on who wins.

Prices come from the real order book, never from the gamma quote fields. Those
are last/mid prices, and an "edge" computed from a midpoint is an edge that
does not exist at the touch.

READ-ONLY. This command reports; it never places an order, and nothing in the
package will execute these for you. Two reasons:

  * Both legs must fill. Polymarket has no atomic two-leg order, so a fill on
    one side and a miss on the other converts a "risk-free" trade into a naked
    directional position at a price nobody chose.
  * Taker fees reach 5% on some markets, which is larger than essentially any
    edge this finder can detect. --fee sets the assumption; the report says
    plainly when fees eat the trade.

Exit code 0 when the scan ran (including "found nothing", which is the normal
answer), 1 when it failed.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.arbitrage import DEFAULT_TAKER_FEE
from polymarket_bot.scripts._common import (
    add_json_flag,
    build_parser,
    dispatch,
    emit,
    print_response,
    with_disclaimer,
)


def _run(args: argparse.Namespace) -> int:
    if args.maker:
        response = service.maker_pairs(scan_limit=args.scan, max_books=args.books)
    else:
        response = service.arbitrage(
            scan_limit=args.scan, max_books=args.books, fee_rate=args.fee
        )

    if args.json:
        return print_response(response, as_json=True)

    if not response.get("ok"):
        emit(f"Scan failed: {response.get('error')}")
        return 1

    emit(with_disclaimer(str(response.get("text") or "")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "arbitrage",
        "Find YES+NO pairs priced under the $1 they redeem for. Read-only.",
        epilog=__doc__,
    )
    parser.add_argument(
        "--scan",
        type=int,
        default=120,
        help="How many live markets to pull, ordered by 24h volume (default 120).",
    )
    parser.add_argument(
        "--books",
        type=int,
        default=40,
        help="How many of those to actually price. Each costs two order-book reads (default 40).",
    )
    parser.add_argument(
        "--fee",
        type=float,
        default=DEFAULT_TAKER_FEE,
        help=f"Assumed taker fee, as a fraction (default {DEFAULT_TAKER_FEE}). Some markets charge 0.05.",
    )
    parser.add_argument(
        "--maker",
        action="store_true",
        help=(
            "Scan the BID side instead: pairs you could buy cheap by RESTING a "
            "limit on both sides. Common, unlike taker arb - but the fills are "
            "not guaranteed. Ignores --fee (a maker pays no taker fee)."
        ),
    )
    add_json_flag(parser)

    args = parser.parse_args(argv)
    if args.scan < 1:
        parser.error("--scan must be at least 1.")
    if args.books < 1:
        parser.error("--books must be at least 1.")
    if not (0 <= args.fee < 1):
        parser.error("--fee is a fraction between 0 and 1 (0.05 means 5%).")
    return dispatch(_run, args)


if __name__ == "__main__":
    raise SystemExit(main())
