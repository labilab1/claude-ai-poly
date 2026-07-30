"""Preview - or, deliberately, place - one small order.

DRY-RUN BY DEFAULT. Without --execute this prices the order, runs every risk
check, prints exactly what would happen, and sends nothing.

Buying:
  # Preview a $1 buy of YES (safe, nothing placed):
  python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --usd 1

  # The other outcome:
  python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side no --usd 1

  # A resting bid instead of crossing the spread:
  python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --usd 1 --limit-price 0.42

Selling what you hold:
  python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --sell --shares 10
  python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --sell --all
  python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --sell --all --limit-price 0.80

Actually placing it:
  # ... --execute      then type EXECUTE at the prompt
  # ... --execute --yes  non-interactive, for a trade already approved elsewhere

A real order needs --execute *and* a confirmation. Risk limits (per-market cap,
cash reserve, and the per-order cap - which applies to BUYs only, because a sell
reduces risk) are enforced in the core trading layer, not here: a blocked plan is
refused whatever flags are passed.

What you confirm is what gets sent. The order is executed from the plan that was
printed - the exact share count, not "--all" again - and the confirmed size is
passed back to the service as `expected_shares`/`expected_usdc`, which refuses if
live state has grown the order since the preview. Rebuilding the request from
argv is how a confirmation for 10.00 shares turns into a sale of 15.00 when a
resting buy fills while you are reading the prompt.

Exit codes: 0 when the preview is clean or the order went through, 1 when the
plan is blocked, refused, cancelled, or the order failed.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.scripts._common import (
    add_json_flag,
    build_parser,
    confirm_execution,
    dispatch,
    dump_json,
    emit,
    with_disclaimer,
)


def _summarise(plan: dict, market_ref: str) -> str:
    """One line stating precisely what is about to be spent or sold."""
    outcome = plan.get("outcome_label") or market_ref
    limit = plan.get("limit_price")
    leg = f" as a resting limit order at {limit:.4f}" if limit is not None else " at the market price"
    if plan.get("side") == "BUY":
        amount = plan.get("usdc_amount") or 0.0
        return f"REAL ORDER: spend ${amount:.2f} USDC buying '{outcome}'{leg}."
    shares = plan.get("shares") or 0.0
    proceeds = plan.get("est_proceeds")
    tail = f", expecting about ${proceeds:.2f} back" if proceeds is not None else ""
    return (
        f"REAL ORDER: sell {shares:,.2f} '{outcome}' shares{leg}{tail}.\n"
        f"Confirming binds this exact size: if the position has grown by the time it is "
        f"sent, the order is refused, not resized."
    )


def _preview(args: argparse.Namespace) -> dict:
    if args.sell:
        return service.preview_sell(
            args.market,
            args.side,
            shares=args.shares,
            fraction=1.0 if args.all else None,
            limit_price=args.limit_price,
        )
    return service.preview_buy(
        args.market,
        args.side,
        args.usd if args.usd is not None else 1.0,
        limit_price=args.limit_price,
    )


def _confirmed_size(plan: dict, *, selling: bool) -> float | None:
    """The one number the confirmation is about: shares on a sell, USDC on a buy."""
    value = plan.get("shares") if selling else plan.get("usdc_amount")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if float(value) > 0 else None


def _execute(args: argparse.Namespace, plan: dict, size: float) -> dict:
    """Send exactly the plan that was previewed and confirmed.

    Built from `plan`, never from argv. Rebuilding it from `--all` would make a
    fresh plan whose size is re-resolved against the live position, so shares
    bought (or a resting order filled) between the preview and the typed
    EXECUTE would be sold too - shares the confirmation never mentioned.
    `expected_shares`/`expected_usdc` carry the confirmed number into the
    service, which refuses to send anything larger.
    """
    if args.sell:
        return service.sell(
            args.market,
            args.side,
            shares=size,
            limit_price=plan.get("limit_price"),
            confirm=True,
            expected_shares=size,
        )
    return service.buy(
        args.market,
        args.side,
        size,
        limit_price=plan.get("limit_price"),
        confirm=True,
        expected_usdc=size,
    )


def _run(args: argparse.Namespace) -> int:
    preview = _preview(args)

    if args.json and not args.execute:
        dump_json(preview)
        return 0 if preview.get("ok") and preview.get("executable") else 1

    if not args.json:
        emit(with_disclaimer(str(preview.get("text") or "")))

    if not preview.get("ok"):
        return 1

    if not args.execute:
        if not args.json:
            emit("")
            emit("DRY RUN - nothing was sent. Add --execute to place this for real.")
        return 0 if preview.get("executable") else 1

    if not preview.get("executable"):
        # The service would refuse this anyway; stopping here avoids prompting
        # for a confirmation that could never be acted on.
        emit("")
        emit("Refusing to execute: the plan above is blocked. Nothing sent.")
        return 1

    plan = preview.get("plan") or {}
    size = _confirmed_size(plan, selling=args.sell)
    if size is None:
        # Without a size from the plan there is nothing to bind the confirmation
        # to, and falling back to argv is exactly the bug this avoids.
        emit("")
        emit("Refusing to execute: the preview did not state a size to confirm. Nothing sent.")
        return 1

    if not confirm_execution(_summarise(plan, args.market), assume_yes=args.yes):
        return 1

    emit("")
    emit("Sending order...")
    result = _execute(args, plan, size)

    if args.json:
        dump_json(result)
    else:
        emit(str(result.get("text") or ""))
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "place_test_order",
        "Preview (default) or place one small order. Buying, selling, market or limit.",
        epilog=__doc__,
    )
    parser.add_argument("--market", required=True, help="Condition id (0x...), slug, or market URL.")
    parser.add_argument(
        "--side",
        "--outcome",
        dest="side",
        required=True,
        help="Which outcome: yes/no, or the market's own label (Up, Down, ...).",
    )
    parser.add_argument("--usd", type=float, help="BUY only: USDC to spend (default 1.00).")
    parser.add_argument("--sell", action="store_true", help="Sell an outcome you hold instead of buying.")
    parser.add_argument("--shares", type=float, help="SELL only: how many shares to sell.")
    parser.add_argument("--all", action="store_true", help="SELL only: sell the entire holding.")
    parser.add_argument(
        "--limit-price",
        dest="limit_price",
        type=float,
        help="Rest a limit order at this price (0..1) instead of taking the market price.",
    )
    parser.add_argument("--execute", action="store_true", help="Actually place the order (still needs confirmation).")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive EXECUTE prompt (the caller has already approved this exact trade).",
    )
    add_json_flag(parser)

    args = parser.parse_args(argv)

    # Argument shape only - anything about money or market state is decided by
    # the trading layer, which re-checks it against live state before sending.
    if args.sell:
        if args.usd is not None:
            parser.error("--usd applies to a BUY; a SELL is sized with --shares or --all.")
        if bool(args.shares is not None) == bool(args.all):
            parser.error("A SELL needs exactly one of --shares or --all.")
        if args.shares is not None and args.shares <= 0:
            parser.error("--shares must be greater than zero.")
    else:
        if args.shares is not None or args.all:
            parser.error("--shares and --all apply to a SELL; add --sell, or use --usd to buy.")
        if args.usd is not None and args.usd <= 0:
            parser.error("--usd must be greater than zero.")
    if args.json and args.execute and not args.yes:
        parser.error("--json with --execute needs --yes: the confirmation prompt would corrupt the JSON output.")

    return dispatch(_run, args)


if __name__ == "__main__":
    raise SystemExit(main())
