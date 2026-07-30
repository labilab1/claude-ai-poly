"""Read-only "where is my money?" diagnostic.

    python -m polymarket_bot.scripts.wallet_status
    python -m polymarket_bot.scripts.wallet_status --skip-chains
    python -m polymarket_bot.scripts.wallet_status --json

Shows two different things, and the difference between them is the point:

  1. native gas + USDC held by the signer EOA on the major chains - this is what
     catches a deposit stranded on the wrong network or in the wrong USDC
     contract, the classic Polymarket funding mistake;
  2. the Polymarket account behind the configured deposit wallet, which is the
     balance that can actually trade.

The chain sweep is plain JSON-RPC and lives here rather than in the core
package: it is a wallet diagnostic, not part of the trading path, and nothing
in `service` exposes it. Exit code 1 means the Polymarket side could not be
read (a chain RPC failing on its own is reported inline and is not fatal).
"""

from __future__ import annotations

import argparse

import requests
from eth_account import Account

from polymarket_bot import service
from polymarket_bot.config import load_private_key, load_settings
from polymarket_bot.scripts._common import (
    build_parser,
    dispatch,
    dump_json,
    emit,
    print_response,
)

# (name, rpc, native symbol, usdc contract)
NETWORKS = [
    ("Ethereum", "https://ethereum-rpc.publicnode.com", "ETH", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    ("Base",     "https://base-rpc.publicnode.com",      "ETH", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    ("Arbitrum", "https://arbitrum-one-rpc.publicnode.com","ETH","0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    ("Optimism", "https://optimism-rpc.publicnode.com",   "ETH", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
    ("Polygon-USDC.e", "https://polygon-bor-rpc.publicnode.com", "POL", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
    ("Polygon-USDC",   "https://polygon-bor-rpc.publicnode.com", "POL", "0x3c499c542cEF5E3811e1192ce70d8cc03d5c3359"),
]

# ERC-20 balanceOf(address) selector.
_BALANCE_OF = "0x70a08231"


def _rpc(rpc: str, method: str, params: list, timeout: float) -> str:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = requests.post(rpc, json=payload, timeout=timeout).json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def _chain_rows(address: str, timeout: float) -> list[dict]:
    rows: list[dict] = []
    for name, rpc, symbol, usdc in NETWORKS:
        try:
            native = int(_rpc(rpc, "eth_getBalance", [address, "latest"], timeout), 16) / 1e18
            data = _BALANCE_OF + address.lower().replace("0x", "").rjust(64, "0")
            balance = int(_rpc(rpc, "eth_call", [{"to": usdc, "data": data}, "latest"], timeout), 16) / 1_000_000
            rows.append({"network": name, "symbol": symbol, "native": native, "usdc": balance, "error": None})
        except Exception as exc:
            rows.append({"network": name, "symbol": symbol, "native": None, "usdc": None, "error": str(exc)[:80]})
    return rows


def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    signer = Account.from_key(load_private_key()).address
    chains = [] if args.skip_chains else _chain_rows(signer, args.timeout)
    account = service.status()

    if args.json:
        dump_json({"signer": signer, "deposit_wallet": settings.wallet, "chains": chains, "account": account})
        return 0 if account.get("ok") else 1

    emit(f"Signer (EOA)              : {signer}")
    emit(f"Configured deposit wallet : {settings.wallet}")
    emit("These are usually different addresses - the deposit wallet is the proxy")
    emit("that actually holds the USDC the bot trades with.")
    emit("")

    if chains:
        emit("On-chain balances (signer EOA):")
        for row in chains:
            if row["error"]:
                emit(f"  {row['network']:<16} | RPC ERROR: {row['error']}")
            else:
                emit(f"  {row['network']:<16} | {row['symbol']} {row['native']:.5f} | USDC {row['usdc']:.2f}")
        emit("")

    emit("Polymarket account (deposit wallet):")
    if not account.get("ok"):
        emit(f"  could not be read: {account.get('error')}")
        return 1
    return print_response(account)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "wallet_status",
        "Read-only balance diagnostic: on-chain funds plus the Polymarket account.",
    )
    parser.add_argument("--skip-chains", action="store_true", help="Only read the Polymarket account (much faster).")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-RPC timeout in seconds (default 20).")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw data as JSON instead of a report.",
    )
    return dispatch(_run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
