"""What you are holding, what it is worth, and what it cost.

    python -m polymarket_bot.scripts.portfolio
    python -m polymarket_bot.scripts.portfolio --hide-settled
    python -m polymarket_bot.scripts.portfolio --json

Read-only: it prices positions, it never trades or redeems anything.

Two sections, because they mean different things. Open positions can still move
and can be sold. Settled positions cannot - the market has resolved and the only
action left is redemption, which pays $1.00 per winning share and $0.00 per
losing one. A lost position stays flagged "redeemable" forever; claiming it
clears the row and returns nothing.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.client import get_client
from polymarket_bot.scripts._common import (
    Column,
    add_json_flag,
    build_parser,
    dispatch,
    dump_json,
    emit,
    money,
    price,
    render_table,
    signed,
    with_disclaimer,
)

_TITLE_WIDTH = 44

_OPEN_COLUMNS = (
    Column("MARKET", max_width=_TITLE_WIDTH),
    Column("OUTCOME", max_width=10),
    Column("SHARES", right=True),
    Column("ENTRY", right=True),
    Column("NOW", right=True),
    Column("COST", right=True),
    Column("VALUE", right=True),
    Column("P&L", right=True),
    Column("P&L %", right=True),
)

_SETTLED_COLUMNS = (
    Column("MARKET", max_width=_TITLE_WIDTH),
    Column("OUTCOME", max_width=10),
    Column("SHARES", right=True),
    Column("ENTRY", right=True),
    Column("COST", right=True),
    Column("REDEEMS", right=True),
    Column("RESULT", max_width=14),
)


def _is_settled(position: dict) -> bool:
    """Resolved, or claimable: either way it can be redeemed but not sold."""
    return bool(position.get("is_resolved") or position.get("redeemable"))


def _open_row(position: dict) -> list[str]:
    return [
        position.get("market_title") or "",
        position.get("outcome") or "",
        f"{position.get('shares', 0.0):,.2f}",
        price(position.get("avg_price", 0.0)),
        price(position.get("cur_price", 0.0)),
        money(position.get("cost_basis", 0.0)),
        money(position.get("current_value", 0.0)),
        signed(position.get("unrealized_pnl", 0.0)),
        f"{position.get('unrealized_pnl_pct', 0.0):+.1f}%",
    ]


def _settled_row(position: dict) -> list[str]:
    payout = float(position.get("current_value", 0.0))
    return [
        position.get("market_title") or "",
        position.get("outcome") or "",
        f"{position.get('shares', 0.0):,.2f}",
        price(position.get("avg_price", 0.0)),
        money(position.get("cost_basis", 0.0)),
        money(payout),
        "won" if payout > 0 else "lost - pays $0",
    ]


def _run(args: argparse.Namespace) -> int:
    # One connection for both reads: the totals come from the account summary,
    # the rows from the position list, and they should describe the same instant.
    client = get_client()
    try:
        account = service.status(client=client)
        listing = service.positions(include_resolved=True, client=client)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if args.json:
        dump_json({"account": account, "positions": listing})
        return 0 if account.get("ok") and listing.get("ok") else 1

    if not account.get("ok"):
        emit(f"Could not read the account: {account.get('error')}")
        return 1
    if not listing.get("ok"):
        emit(f"Could not read positions: {listing.get('error')}")
        return 1

    summary = account.get("portfolio") or {}
    everything: list[dict] = list(listing.get("positions") or [])
    # "Settled" covers both flags: a resolved market and anything the exchange
    # will let us redeem. Either way it cannot be sold, only claimed.
    settled = [p for p in everything if _is_settled(p)]
    live = [p for p in everything if not _is_settled(p)]
    live.sort(key=lambda p: p.get("current_value", 0.0), reverse=True)
    settled.sort(key=lambda p: p.get("cost_basis", 0.0), reverse=True)

    lines: list[str] = [f"OPEN POSITIONS ({len(live)})"]
    if live:
        lines.extend(render_table(_OPEN_COLUMNS, [_open_row(p) for p in live]))
        lines.append(
            f"  Open value {money(sum(p.get('current_value', 0.0) for p in live))}"
            f" | cost {money(sum(p.get('cost_basis', 0.0) for p in live))}"
            f" | unrealized {signed(sum(p.get('unrealized_pnl', 0.0) for p in live))}"
        )
    else:
        lines.append("  None - no live holdings to price or sell.")

    # The summary splits the book in two and the field names say which half is
    # which: `unrealized_pnl` is live positions only, `settled_pnl` is the
    # decided-but-unclaimed half. Printing the live number alone is what used to
    # produce "open positions 0, unrealized -13.93", which reads as a
    # contradiction and invites waiting for a recovery that cannot happen.
    lines.append("")
    lines.append("PORTFOLIO")
    lines.append(f"  Cash (USDC)      : {money(summary.get('cash_usdc', 0.0)):>12}")
    lines.append(
        f"  Open positions   : {money(summary.get('open_positions_value', 0.0)):>12}"
        f"   ({summary.get('open_positions', 0)} still tradable)"
    )
    lines.append(
        f"  Settled value    : {money(summary.get('settled_value', 0.0)):>12}"
        f"   ({summary.get('settled_positions', 0)} resolved, redeem only)"
    )
    lines.append(f"  Positions value  : {money(summary.get('positions_value', 0.0)):>12}   (open + settled)")
    lines.append(f"  Total value      : {money(summary.get('total_value', 0.0)):>12}")
    lines.append(f"  Unrealized P&L   : {signed(summary.get('unrealized_pnl', 0.0)):>12}   (open positions - can still move)")
    lines.append(f"  Settled P&L      : {signed(summary.get('settled_pnl', 0.0)):>12}   (decided; redeeming books it)")
    lines.append(f"  Total P&L        : {signed(summary.get('total_pnl', 0.0)):>12}   (open + settled)")
    lines.append(f"  Realized P&L     : {signed(summary.get('realized_pnl', 0.0)):>12}")

    if settled and not args.hide_settled:
        redeem_value = sum(p.get("current_value", 0.0) for p in settled)
        cost = sum(p.get("cost_basis", 0.0) for p in settled)
        winners = [p for p in settled if p.get("current_value", 0.0) > 0]
        lines.append("")
        lines.append(f"SETTLED / REDEEMABLE ({len(settled)})")
        lines.extend(render_table(_SETTLED_COLUMNS, [_settled_row(p) for p in settled]))
        lines.append(f"  Cost {money(cost)} -> redeems for {money(redeem_value)} ({len(winners)} winner(s)).")
        if redeem_value < 0.01:
            lines.append("  Every one of these resolved against the position. They are listed as")
            lines.append("  redeemable because the exchange always is - claiming them pays $0.00 and")
            lines.append("  only clears the list. There is no money to recover here.")
        else:
            lines.append("  Only rows with REDEEMS above $0.00 return money; the rest pay $0.00 and")
            lines.append("  are cleared, not recovered.")
        lines.append("  This command does not redeem anything.")
    elif settled:
        lines.append("")
        lines.append(f"  ({len(settled)} settled position(s) hidden by --hide-settled.)")

    emit(with_disclaimer("\n".join(lines)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "portfolio",
        "Open positions with entry/current price, value and unrealized P&L, plus settled holdings.",
        epilog=__doc__,
    )
    parser.add_argument(
        "--hide-settled",
        action="store_true",
        help="Skip the settled/redeemable section and show live holdings only.",
    )
    add_json_flag(parser)
    return dispatch(_run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
