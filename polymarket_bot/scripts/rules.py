"""Manage exit rules - the take-profit / stop-loss / trailing exits the monitor enforces.

    python -m polymarket_bot.scripts.rules list
    python -m polymarket_bot.scripts.rules list --active

    python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind take_profit --pct 50
    python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind stop_loss --pct -30
    python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind trailing_stop --trail 20
    python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind take_profit --price 0.80
    python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind time_exit --expires 2026-08-01T12:00:00Z

    python -m polymarket_bot.scripts.rules remove <rule_id>

`--market` takes a condition id (0x...), a slug, or a pasted market URL.
`--pct` is measured against your average entry price: +50 on a take-profit means
sell at 1.5x entry, -30 on a stop-loss means sell at 0.7x entry. `--trail` is
measured against the highest price seen since the rule was created.

Adding a rule reads your live position first and refuses if you do not hold the
outcome - a rule that has nothing to sell is not protection.

None of these exist on Polymarket. The exchange has no native stop-loss, so
every rule here fires only while `scripts.monitor` is actually running, and only
sends orders when the monitor runs with --execute. Storing a rule is local
bookkeeping; nothing is placed on the exchange by this command.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.rules import VALID_KINDS
from polymarket_bot.scripts._common import add_json_flag, build_parser, dispatch, print_response


def _list(args: argparse.Namespace) -> int:
    return print_response(service.list_rules(active_only=args.active), as_json=args.json)


def _add(args: argparse.Namespace) -> int:
    response = service.set_rule(
        args.market,
        args.side,
        args.kind,
        target_price=args.price,
        target_pct=args.pct,
        trail_pct=args.trail,
        exit_fraction=args.fraction,
        expires_at=args.expires,
        note=args.note,
    )
    return print_response(response, as_json=args.json, disclaimer=True)


def _remove(args: argparse.Namespace) -> int:
    return print_response(service.remove_rule(args.rule_id), as_json=args.json)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "rules",
        "List, add and remove the exit rules the monitor enforces.",
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="Show stored exit rules.")
    listing.add_argument("--active", action="store_true", help="Only rules that are still armed.")
    add_json_flag(listing)
    listing.set_defaults(handler=_list)

    adding = subparsers.add_parser(
        "add",
        help="Attach an exit rule to a position you hold.",
        description="Attach an exit rule to a position you hold. Nothing is sent to the exchange.",
    )
    adding.add_argument("--market", required=True, help="Condition id (0x...), slug, or market URL.")
    adding.add_argument(
        "--side",
        "--outcome",
        dest="side",
        required=True,
        help="Which outcome you hold: yes/no, or the market's own label (Up, Down, ...).",
    )
    adding.add_argument(
        "--kind",
        required=True,
        choices=list(VALID_KINDS),
        help="take_profit and stop_loss need --pct or --price; trailing_stop needs --trail; time_exit needs --expires.",
    )
    adding.add_argument("--pct", type=float, help="Trigger as a percent of your entry price (+50 / -30).")
    adding.add_argument("--price", type=float, help="Trigger as an absolute price in 0..1 (e.g. 0.80).")
    adding.add_argument("--trail", type=float, help="trailing_stop only: percent below the highest price seen.")
    adding.add_argument(
        "--fraction",
        type=float,
        default=1.0,
        help="Portion of the holding to sell when it fires, 0-1 (default 1.0 = all of it).",
    )
    adding.add_argument("--expires", help="time_exit only: ISO timestamp, e.g. 2026-08-01T12:00:00Z.")
    adding.add_argument("--note", help="Free-text reminder stored with the rule.")
    add_json_flag(adding)
    adding.set_defaults(handler=_add)

    removing = subparsers.add_parser("remove", help="Delete one stored rule by id.")
    removing.add_argument("rule_id", help="Rule id as shown by `rules list`.")
    add_json_flag(removing)
    removing.set_defaults(handler=_remove)

    args = parser.parse_args(argv)
    return dispatch(args.handler, args)


if __name__ == "__main__":
    raise SystemExit(main())
