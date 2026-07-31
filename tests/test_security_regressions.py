"""Regression tests for issues found in the security review of the menu work.

Each of these was a real defect in code that shipped, not a hypothetical:

  1. The bot token rode inside the request URL, and `requests` puts the URL
     into every transport exception it raises. Those exceptions were logged
     and shown in chat, writing a working token into log files.
  2. "Cancel orders" executed on a single tap, next to read-only buttons,
     destroying any take-profit resting on the book with no confirmation.
  3. The buy-amount prompt accepted "nan" and "inf".
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from polymarket_bot.config import Settings
from polymarket_bot.telegram import bot as bot_module
from polymarket_bot.telegram import menu as menu_mod
from polymarket_bot.telegram.api import TelegramAPI, TelegramError
from polymarket_bot.telegram.bot import TelegramBot

TOKEN = "123456789:AAHfake-Token-Value-Do-Not-Use-abcdefghij"
OWNER = 12345


# ---------------------------------------------------------------------------
# 1. token redaction
# ---------------------------------------------------------------------------


def _api_raising(exc):
    api = TelegramAPI(TOKEN)
    session = mock.Mock()
    session.post.side_effect = exc
    api._session = session
    return api


def test_a_connection_error_does_not_carry_the_token():
    """requests embeds the full URL in its message, and the URL contains the
    token. This is the exact string that reached the log file."""
    realistic = requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): "
        f"Max retries exceeded with url: /bot{TOKEN}/sendMessage"
    )
    api = _api_raising(realistic)
    with pytest.raises(TelegramError) as info:
        api.send_message(1, "hello")
    assert TOKEN not in str(info.value)
    assert "REDACTED" in str(info.value)


def test_a_timeout_does_not_carry_the_token():
    api = _api_raising(requests.exceptions.Timeout(f"timed out for /bot{TOKEN}/getUpdates"))
    with pytest.raises(TelegramError) as info:
        api.get_updates()
    assert TOKEN not in str(info.value)


def test_the_original_exception_is_not_chained_into_the_traceback():
    """`raise ... from None` matters here: a chained __cause__ would print the
    original token-bearing message in any traceback."""
    api = _api_raising(requests.exceptions.ConnectionError(f"url /bot{TOKEN}/x"))
    with pytest.raises(TelegramError) as info:
        api.send_message(1, "hi")
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_a_token_echoed_in_an_api_error_description_is_redacted():
    api = TelegramAPI(TOKEN)
    session = mock.Mock()
    session.post.return_value = SimpleNamespace(
        json=lambda: {"ok": False, "description": f"bad token {TOKEN}"},
        status_code=401,
        text="",
    )
    api._session = session
    with pytest.raises(TelegramError) as info:
        api.send_message(1, "hi")
    assert TOKEN not in str(info.value)


def test_a_non_json_body_is_redacted_too():
    api = TelegramAPI(TOKEN)
    session = mock.Mock()
    session.post.return_value = SimpleNamespace(
        json=mock.Mock(side_effect=ValueError("no json")),
        status_code=502,
        text=f"<html>proxy error for /bot{TOKEN}/sendMessage</html>",
    )
    api._session = session
    with pytest.raises(TelegramError) as info:
        api.send_message(1, "hi")
    assert TOKEN not in str(info.value)


def test_redaction_survives_a_message_that_never_mentions_the_token():
    api = _api_raising(requests.exceptions.ConnectionError("network unreachable"))
    with pytest.raises(TelegramError) as info:
        api.send_message(1, "hi")
    assert "network unreachable" in str(info.value)


# ---------------------------------------------------------------------------
# 2 & 3. bot-level guards
# ---------------------------------------------------------------------------


class FakeAPI:
    def __init__(self):
        self.sent: list[dict] = []
        self.acks: list[dict] = []

    def get_me(self):
        return {"username": "b"}

    def get_updates(self, *, offset=None, timeout=25):
        return []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        return {}

    def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        self.acks.append({"text": text})
        return {}

    def all_text(self):
        return "\n".join(s["text"] for s in self.sent)

    def last_inline(self):
        for entry in reversed(self.sent):
            markup = entry.get("reply_markup") or {}
            if "inline_keyboard" in markup:
                return markup["inline_keyboard"]
        return []


@pytest.fixture
def bot_and_api(tmp_path):
    api = FakeAPI()
    settings = Settings(
        private_key="0x0", wallet="0xw", data_dir=tmp_path,
        telegram_bot_token="tok", telegram_chat_id=str(OWNER),
    )
    return TelegramBot(settings, client=SimpleNamespace(), api=api), api


def _cb(data):
    return {"callback_query": {"id": "cb", "data": data,
                               "message": {"chat": {"id": OWNER}, "message_id": 1}}}


def _msg(text):
    return {"message": {"chat": {"id": OWNER}, "text": text}}


def test_tapping_cancel_orders_asks_before_doing_anything(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "cancel_orders", side_effect=AssertionError("cancelled on one tap")
    ):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MORE}:cancel"))
    # And it says what is being given up.
    assert "take-profit" in api.all_text().lower()
    assert api.last_inline(), "no confirmation buttons were offered"


def test_the_confirmed_form_actually_cancels(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "cancel_orders",
        return_value={"ok": True, "canceled_count": 2, "not_canceled": {}},
    ) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MORE}:cancel!"))
    assert spy.called
    # Rendered from the structured count, in the owner's language.
    assert "2" in api.all_text()


def test_declining_the_confirmation_returns_to_the_menu_without_cancelling(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "cancel_orders", side_effect=AssertionError("must not cancel")
    ):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MORE}:cancel"))
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MORE}:"))
    assert api.last_inline(), "the More menu did not come back"


def test_a_confirmed_suffix_on_a_read_only_action_is_not_a_bypass(bot_and_api):
    # "!" must not become a way to invoke arbitrary actions.
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "status", side_effect=AssertionError("reached via ! suffix")
    ):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MORE}:status!"))


def test_read_only_actions_still_run_on_one_tap(bot_and_api):
    # The confirmation must not have been applied to everything.
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "status",
        return_value={"ok": True, "portfolio": {"cash_usdc": 21.0}, "limits": {}},
    ) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MORE}:status"))
    assert spy.called and "21.00" in api.all_text()


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_non_finite_buy_amounts_are_rejected_at_the_prompt(bot_and_api, bad):
    """float("nan") passes `<= 0` because every NaN comparison is False, and
    float("inf") passes `> 0`. Both used to reach service.buy."""
    bot, api = bot_and_api
    session = bot._sessions.get(OWNER)
    session.awaiting = menu_mod.AWAIT_BUY_AMOUNT
    session.pending_market = "m"
    session.pending_outcome = "yes"

    with mock.patch.object(
        bot_module.service, "buy", side_effect=AssertionError(f"priced a {bad} order")
    ):
        bot._handle_update(_msg(bad))

    assert session.awaiting == menu_mod.AWAIT_BUY_AMOUNT, "prompt moved on after bad input"


def test_a_normal_amount_still_works(bot_and_api):
    bot, api = bot_and_api
    session = bot._sessions.get(OWNER)
    session.awaiting = menu_mod.AWAIT_BUY_AMOUNT
    session.pending_market = "m"
    session.pending_outcome = "yes"

    with mock.patch.object(bot_module.service, "buy", return_value={"ok": True, "text": "x"}) as spy:
        bot._handle_update(_msg("2.50"))
    assert spy.call_args.args[2] == 2.50


def test_an_absurdly_long_number_does_not_crash_the_prompt(bot_and_api):
    bot, api = bot_and_api
    session = bot._sessions.get(OWNER)
    session.awaiting = menu_mod.AWAIT_BUY_AMOUNT
    session.pending_market = "m"
    session.pending_outcome = "yes"
    with mock.patch.object(bot_module.service, "buy", side_effect=AssertionError("must not price")):
        bot._handle_update(_msg("9" * 400))  # float() gives inf
    assert session.awaiting == menu_mod.AWAIT_BUY_AMOUNT


# ---------------------------------------------------------------------------
# authorization still holds on every new surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        f"nav:{menu_mod.VIEW_ARB}:",
        f"nav:{menu_mod.VIEW_WATCHLIST}:",
        f"nav:{menu_mod.VIEW_WATCH}:tok",
        f"nav:{menu_mod.VIEW_MORE}:cancel!",
        f"nav:{menu_mod.VIEW_LANG}:",
    ],
)
def test_every_new_callback_surface_rejects_a_stranger(bot_and_api, payload):
    bot, api = bot_and_api
    update = {
        "callback_query": {
            "id": "cb", "data": payload,
            "message": {"chat": {"id": 999}, "message_id": 1},
        }
    }
    for name in ("cancel_orders", "arbitrage", "briefing", "scan"):
        if not hasattr(bot_module.service, name):
            continue
    with mock.patch.object(bot_module.service, "cancel_orders", side_effect=AssertionError("ran")), \
         mock.patch.object(bot_module.service, "arbitrage", side_effect=AssertionError("ran")):
        bot._handle_update(update)
    assert api.sent == []
