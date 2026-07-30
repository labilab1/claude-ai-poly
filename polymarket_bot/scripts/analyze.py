"""Your trading record, what it shows, and which markets are cheapest to trade.

    python -m polymarket_bot.scripts.analyze
    python -m polymarket_bot.scripts.analyze --limit 25
    python -m polymarket_bot.scripts.analyze --keyword bitcoin
    python -m polymarket_bot.scripts.analyze --stats-only
    python -m polymarket_bot.scripts.analyze --json

Read-only. Three parts:

  * the record - completed positions and realized P&L, straight from account
    history, wins and losses alike;
  * the insights - patterns in that history, including the unflattering ones;
  * the opportunities - markets ranked on tradability *only*: spread, resting
    liquidity, 24h volume, maker rewards and time to resolution.

The ranking is a statement about execution cost, not about which outcome wins.
Nothing here predicts anything, and a high score is not a reason to buy.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.client import get_client
from polymarket_bot.scripts._common import (
    add_json_flag,
    build_parser,
    dispatch,
    dump_json,
    emit,
    with_disclaimer,
)


def _run(args: argparse.Namespace) -> int:
    want_record = not args.opportunities_only
    want_markets = not args.stats_only

    record: dict = {}
    markets: dict = {}
    # One connection for the whole report - the history sweep and the market
    # scan are both several round trips.
    client = get_client()
    try:
        if want_record:
            record = service.analytics(client=client)
        if want_markets:
            markets = service.opportunities(limit=args.limit, keyword=args.keyword, client=client)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if args.json:
        dump_json({"analytics": record or None, "opportunities": markets or None})
        return 0 if all(r.get("ok") for r in (record, markets) if r) else 1

    blocks: list[str] = []
    if record:
        blocks.append(str(record.get("text") or f"Analytics failed: {record.get('error')}"))
    if markets:
        blocks.append(str(markets.get("text") or f"Opportunity scan failed: {markets.get('error')}"))

    emit(with_disclaimer("\n\n".join(blocks)))
    return 0 if all(r.get("ok") for r in (record, markets) if r) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "analyze",
        "Trade statistics, honest insights from your own history, and the most tradable markets.",
        epilog=__doc__,
    )
    parser.add_argument("--limit", type=int, default=15, help="How many opportunities to rank (default 15).")
    parser.add_argument("--keyword", type=str, help="Only score markets whose question or slug contains this text.")
    parser.add_argument("--stats-only", action="store_true", help="Skip the market scan; show the record and insights.")
    parser.add_argument(
        "--opportunities-only",
        action="store_true",
        help="Skip the history; show the tradability ranking only.",
    )
    add_json_flag(parser)

    args = parser.parse_args(argv)
    if args.stats_only and args.opportunities_only:
        parser.error("--stats-only and --opportunities-only cannot be combined; that asks for nothing.")
    return dispatch(_run, args)


if __name__ == "__main__":
    raise SystemExit(main())
