import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


HOST = os.environ.get("POLYMARKET_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))


def load_credentials() -> dict:
    return {
        "private_key": _require("POLYMARKET_PRIVATE_KEY"),
        "api_key": _require("POLYMARKET_API_KEY"),
        "api_secret": _require("POLYMARKET_API_SECRET"),
        "api_passphrase": _require("POLYMARKET_API_PASSPHRASE"),
    }
