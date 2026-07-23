# polymarket_bot

Scaffold for a Polymarket trading bot using the official `py-clob-client` SDK.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`:
   ```
   cp .env.example .env
   ```

3. Fill in `.env` with your real values (do this in your own editor/terminal —
   never paste secrets into a chat session):
   - `POLYMARKET_PRIVATE_KEY` — your wallet's private key, used to sign orders.
   - `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`
     — the three CLOB API credentials Polymarket issued you.

   `.env` is git-ignored, so it will never be committed.

4. Verify the connection:
   ```
   python -m polymarket_bot.scripts.check_connection
   ```
   This confirms the API is reachable and prints your wallet address and
   balance/allowance — no trading, just a read-only sanity check.

## Layout

- `polymarket_bot/config.py` — loads credentials from environment variables.
- `polymarket_bot/client.py` — builds an authenticated `ClobClient`.
- `polymarket_bot/scripts/check_connection.py` — connection smoke test.

Trading logic (order placement, strategy) isn't implemented yet — this is
just the connection layer.
