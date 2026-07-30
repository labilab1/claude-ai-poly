"""Read-only market scanner / advisor CLI.

Examples:
  # Scan tradable markets, tightest spread first:
  python -m polymarket_bot.scripts.market_snapshot
  python -m polymarket_bot.scripts.market_snapshot --limit 30

  # Filter by keyword:
  python -m polymarket_bot.scripts.market_snapshot --search iran

  # Deep-dive one market by condition id (0x...), slug, or pasted URL:
  python -m polymarket_bot.scripts.market_snapshot --market 0xabc123...
  python -m polymarket_bot.scripts.market_snapshot --market some-market-slug

Nothing here places an order. The ranking is by spread - how cheap a market is
to get into and out of - which is tradability, not a view on which side wins.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.scripts._common import add_json_flag, build_parser, dispatch, print_response


def _run(args: argparse.Namespace) -> int:
    if args.market:
        response = service.briefing(args.market)
    else:
        response = service.scan(limit=args.limit, keyword=args.search)
    return print_response(response, as_json=args.json, disclaimer=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "market_snapshot",
        "Scan tradable markets, or deep-dive one of them. Read-only.",
        epilog=__doc__,
    )
    parser.add_argument("--limit", type=int, default=15, help="How many markets to list (default 15).")
    parser.add_argument("--market", type=str, help="Condition id (0x...), slug, or market URL to deep-dive.")
    parser.add_argument(
        "--search",
        "--keyword",
        dest="search",
        type=str,
        help="Only list markets whose question or slug contains this text.",
    )
    add_json_flag(parser)
    return dispatch(_run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
