"""Sanity check: confirms credentials work and prints basic account info.

Run with: python -m polymarket_bot.scripts.check_connection
"""

from polymarket_bot.client import get_client


def main() -> None:
    client = get_client()

    ok = client.get_ok()
    print(f"API reachable: {ok}")

    address = client.get_address()
    print(f"Wallet address: {address}")

    balance = client.get_balance_allowance()
    print(f"Balance/allowance: {balance}")


if __name__ == "__main__":
    main()
