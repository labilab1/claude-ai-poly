"""Sanity check: confirms credentials work and shows the account.

    python -m polymarket_bot.scripts.check_connection
    python -m polymarket_bot.scripts.check_connection --json

Read-only - it never places an order. It proves the API is reachable, that the
private key plus deposit wallet authenticate, and that we can read the real
balance and positions behind them. Exit code 1 means the account could not be
read, which is the signal a setup step is still wrong.

If the balance looks wrong rather than missing, `wallet_status` is the next
stop: it checks on-chain balances too and catches money sitting on the wrong
chain or in a wallet this key does not control.
"""

from __future__ import annotations

import argparse

from polymarket_bot import service
from polymarket_bot.config import load_settings
from polymarket_bot.scripts._common import (
    add_json_flag,
    build_parser,
    dispatch,
    emit,
    print_response,
)


def _mask(address: str) -> str:
    # Public address, but there is no reason to splatter it across a shared
    # terminal - the first/last characters are enough to spot a wrong wallet.
    return address if len(address) <= 14 else f"{address[:8]}...{address[-6:]}"


def _run(args: argparse.Namespace) -> int:
    response = service.status()

    if args.json:
        return print_response(response, as_json=True)

    settings = load_settings()
    if not response.get("ok"):
        emit("CONNECTION FAILED")
        emit(f"  Deposit wallet : {_mask(settings.wallet)}")
        emit(f"  Error          : {response.get('error')}")
        emit("  Check POLYMARKET_PRIVATE_KEY and POLYMARKET_WALLET in .env, then retry.")
        return 1

    emit("CONNECTION OK - credentials authenticate and the account reads back.")
    emit(f"  Deposit wallet : {_mask(settings.wallet)}")
    emit("")
    return print_response(response)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "check_connection",
        "Verify Polymarket credentials and show the account (read-only).",
    )
    add_json_flag(parser)
    return dispatch(_run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
