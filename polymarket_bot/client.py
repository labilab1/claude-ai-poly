from polymarket import SecureClient

from polymarket_bot.config import load_private_key, load_wallet


def get_client() -> SecureClient:
    return SecureClient.create(private_key=load_private_key(), wallet=load_wallet())
