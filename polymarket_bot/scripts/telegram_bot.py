"""Run the Telegram front end.

    python -m polymarket_bot.scripts.telegram_bot

First run (no TELEGRAM_CHAT_ID set yet):
  1. Create a bot with @BotFather on Telegram, copy the token it gives you
     into TELEGRAM_BOT_TOKEN in .env.
  2. Run this script, then message your bot anything (e.g. /start).
  3. It replies with your chat id. Put that in TELEGRAM_CHAT_ID in .env.
  4. Restart this script - it now answers only that chat, and only that chat.

Every /buy and /sell shows a preview with Confirm/Cancel buttons first;
nothing is sent from a typed command alone. Read polymarket_bot/telegram/bot.py
for exactly how confirmation is bound to the previewed plan.
"""

from __future__ import annotations

import argparse

from polymarket_bot.config import load_settings
from polymarket_bot.notify import ConsoleNotifier
from polymarket_bot.scripts._common import build_parser, dispatch, emit
from polymarket_bot.telegram.bot import TelegramBot


def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.telegram_bot_token:
        emit("TELEGRAM_BOT_TOKEN is not set in .env - see .env.example.")
        return 1

    bot = TelegramBot(settings, notifier=ConsoleNotifier())
    try:
        bot.run_forever(max_iterations=args.max_iterations)
    except KeyboardInterrupt:
        emit("\nStopped.")
    finally:
        bot.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "telegram_bot",
        "Run the Telegram bot (long-polling). Ctrl-C to stop.",
        epilog=__doc__,
    )
    parser.add_argument(
        "--max-iterations", type=int, default=None,
        help="Stop after this many poll cycles instead of running until Ctrl-C (mainly for testing).",
    )
    return dispatch(_run, parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
