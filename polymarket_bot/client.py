from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from polymarket_bot.config import CHAIN_ID, HOST, load_credentials


def get_client() -> ClobClient:
    creds = load_credentials()

    client = ClobClient(
        host=HOST,
        chain_id=CHAIN_ID,
        key=creds["private_key"],
        creds=ApiCreds(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            api_passphrase=creds["api_passphrase"],
        ),
    )
    return client
