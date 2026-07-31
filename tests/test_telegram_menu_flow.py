"""End-to-end tests for the menu flows through the bot router.

The safety-critical one is `test_buying_from_the_menu_still_requires_confirm`:
the menu is a new way to *reach* service.buy, and it must not become a new way
to authorise it. Everything else here is navigation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot.config import Settings
from polymarket_bot.telegram import bot as bot_module
from polymarket_bot.telegram import menu as menu_mod
from polymarket_bot.telegram.bot import TelegramBot
from polymarket_bot.telegram.i18n import t

OWNER = 12345
STRANGER = 999


class FakeAPI:
    """Tracks the canvas the way Telegram does: sends create it, edits replace
    its contents in place. `screens` is every screen the user actually saw."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.acks: list[dict] = []
        self.screens: list[dict] = []
        self._next_id = 100

    def get_me(self):
        return {"username": "test_bot"}

    def get_updates(self, *, offset=None, timeout=25):
        return []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self._next_id += 1
        entry = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup,
                 "message_id": self._next_id}
        self.sent.append(entry)
        self.screens.append(entry)
        return {"message_id": self._next_id}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        entry = {"chat_id": chat_id, "message_id": message_id, "text": text,
                 "reply_markup": reply_markup}
        self.edits.append(entry)
        self.screens.append(entry)
        return {}

    def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        self.acks.append({"id": callback_query_id, "text": text})
        return {}

    # helpers
    def last_text(self) -> str:
        return self.screens[-1]["text"] if self.screens else ""

    def all_text(self) -> str:
        return "\n".join(s["text"] for s in self.screens)

    def last_inline(self) -> list:
        for entry in reversed(self.screens):
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
    bot = TelegramBot(settings, client=SimpleNamespace(), api=api)
    return bot, api


def _msg(text, chat_id=OWNER):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def _cb(data, chat_id=OWNER, message_id=1):
    return {"callback_query": {"id": "cb", "data": data,
                               "message": {"chat": {"id": chat_id}, "message_id": message_id}}}


def _scan_result(count=7):
    return {
        "ok": True,
        "sort": "hot",
        "markets": [
            {
                "slug": f"market-{i}",
                "condition_id": f"0x{i}",
                "question": f"Will thing {i} happen?",
                "yes": {"label": "Yes", "price": 0.6},
                "spread": 0.012,
                "days_left": 14,
                "volume_24h": 1_000_000 - i,
                "url": f"https://polymarket.com/event/ev/market-{i}",
            }
            for i in range(count)
        ],
        "text": "raw",
    }


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------


def test_start_opens_the_dashboard(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "status",
        return_value={"ok": True, "portfolio": {"cash_usdc": 21.62, "total_value": 21.62,
                                                "open_positions": 0}},
    ):
        bot._handle_update(_msg("/start"))
    assert "21.62" in api.last_text(), "the dashboard did not show the balance"
    markup = api.sent[-1]["reply_markup"] or {}
    assert "inline_keyboard" in markup, "home must be inline so taps post nothing"
    assert "keyboard" not in markup


def test_tapping_hot_renders_a_market_list(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    text = api.all_text()
    assert "Will thing 0 happen?" in text
    assert "60%" in text          # 0.6 implied probability
    assert "1.2c" in text         # spread
    assert "14d left" in text     # horizon
    assert "1,000,000" in text    # 24h volume


def test_a_list_page_offers_details_and_a_link_per_row(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    rows = api.last_inline()
    first = rows[0]
    assert any(b.get("callback_data", "").startswith("nav:mkt:") for b in first)
    assert any(b.get("url", "").startswith("https://polymarket.com/") for b in first)


def test_pagination_moves_to_the_next_page(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result(count=12)):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:1"))
    assert "6. Will thing 5 happen?" in api.all_text()


def test_tapping_details_opens_the_briefing(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    token = api.last_inline()[0][0]["callback_data"].split(":")[-1]

    briefing = {"ok": True, "text": "* Will thing 0 happen?\n  ~60% implied",
                "url": "https://polymarket.com/event/ev/market-0"}
    with mock.patch.object(bot_module.service, "briefing", return_value=briefing) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MARKET}:{token}"))
    assert spy.call_args.args[0] == "market-0", "the token did not resolve to its market"
    assert "60% implied" in api.all_text()


def test_a_token_from_a_previous_run_is_reported_not_crashed(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MARKET}:deadbeef"))
    assert t("msg.expired", "en")[:20] in api.all_text()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_prompts_then_uses_the_typed_keyword(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SEARCH}:"))
    assert "keyword" in api.last_text().lower()

    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result(count=2)) as spy:
        bot._handle_update(_msg("bitcoin"))
    assert spy.call_args.kwargs["keyword"] == "bitcoin"
    assert "bitcoin" in api.all_text()
    assert "2 results" in api.all_text()


def test_a_keyboard_tap_cancels_an_outstanding_prompt(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SEARCH}:"))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    # Navigating away must not be read as the search keyword.
    assert spy.call_args.kwargs["keyword"] is None


def test_an_empty_search_result_explains_itself(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SEARCH}:"))
    with mock.patch.object(
        bot_module.service, "scan", return_value={"ok": True, "markets": [], "text": ""}
    ):
        bot._handle_update(_msg("zzzznotathing"))
    assert "zzzznotathing" in api.all_text()


# ---------------------------------------------------------------------------
# buying from the menu -- the safety-critical path
# ---------------------------------------------------------------------------


def test_buying_from_the_menu_still_requires_confirm(bot_and_api):
    """A menu tap must reach the SAME preview-then-confirm flow as /buy.
    Tapping Buy and typing an amount prices the order and sends nothing."""
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    token = api.last_inline()[0][0]["callback_data"].split(":")[-1]

    # Tap "Buy YES" -> the bot asks for an amount, prices nothing yet.
    with mock.patch.object(bot_module.service, "buy", side_effect=AssertionError("priced too early")):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_BUY}:y{token}"))
    assert "USDC" in api.last_text()

    preview = {
        "ok": False,
        "needs_confirmation": True,
        "confirm_args": {"usd": 2.5, "expected_usdc": 2.5, "limit_price": None, "confirm": True},
        "text": "BUY 2.50 USDC of Yes",
    }
    with mock.patch.object(bot_module.service, "buy", return_value=preview) as spy:
        bot._handle_update(_msg("2.50"))

    # Priced with confirm=False (the default), i.e. nothing sent.
    assert spy.call_args.kwargs.get("confirm", False) is False
    assert spy.call_args.args[0] == "market-0"
    assert spy.call_args.args[1] == "yes"
    # And the owner got a Confirm/Cancel keyboard, with a pending entry behind it.
    buttons = [b["callback_data"] for row in api.last_inline() for b in row]
    assert any(b.startswith("confirm:") for b in buttons)
    assert len(bot._pending) == 1


def test_buy_no_button_selects_the_no_outcome(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    token = api.last_inline()[0][0]["callback_data"].split(":")[-1]

    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_BUY}:n{token}"))
    with mock.patch.object(bot_module.service, "buy", return_value={"ok": True, "text": "x"}) as spy:
        bot._handle_update(_msg("1"))
    assert spy.call_args.args[1] == "no"


@pytest.mark.parametrize("bad", ["abc", "-5", "0", ""])
def test_a_bad_amount_is_rejected_without_pricing_anything(bot_and_api, bad):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    token = api.last_inline()[0][0]["callback_data"].split(":")[-1]
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_BUY}:y{token}"))

    with mock.patch.object(bot_module.service, "buy", side_effect=AssertionError("must not price")):
        bot._handle_update(_msg(bad))
    # Still waiting on a valid amount rather than having priced garbage.
    assert bot._sessions.get(OWNER).awaiting == menu_mod.AWAIT_BUY_AMOUNT


def test_amounts_with_a_currency_symbol_or_comma_are_accepted(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    token = api.last_inline()[0][0]["callback_data"].split(":")[-1]
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_BUY}:y{token}"))

    with mock.patch.object(bot_module.service, "buy", return_value={"ok": True, "text": "x"}) as spy:
        bot._handle_update(_msg("$1,250.50"))
    assert spy.call_args.args[2] == 1250.50


# ---------------------------------------------------------------------------
# language
# ---------------------------------------------------------------------------


def test_language_toggle_switches_and_persists(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_LANG}:"))
    assert bot._sessions.get(OWNER).lang == "he"
    assert "עברית" in api.all_text()

    from polymarket_bot.telegram.i18n import load_lang

    assert load_lang(bot.settings, OWNER) == "he"


def test_after_switching_the_list_renders_in_hebrew(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_LANG}:"))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    assert "מחזור" in api.all_text()


def test_the_previous_languages_buttons_still_work_after_a_toggle(bot_and_api):
    """A toggle does not repaint a keyboard already on someone's screen, so an
    English label must still navigate after switching to Hebrew."""
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_LANG}:"))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan_result()) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    assert spy.called


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------


def test_plain_text_from_a_stranger_is_ignored(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", side_effect=AssertionError("must not run")):
        bot._handle_update(_msg(t("menu.hot", "en"), chat_id=STRANGER))
    assert api.sent == []


def test_a_menu_callback_from_a_stranger_is_ignored(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", side_effect=AssertionError("must not run")):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0", chat_id=STRANGER))
    assert api.sent == []


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


def test_a_failing_service_call_reports_instead_of_crashing_the_router(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", side_effect=RuntimeError("api down")):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    assert "api down" in api.all_text()


def test_a_service_response_with_ok_false_is_surfaced(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "scan", return_value={"ok": False, "error": "rate limited"}
    ):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    assert "rate limited" in api.all_text()


def test_unprompted_plain_text_re_opens_the_dashboard(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "status", return_value={"ok": True, "portfolio": {}}):
        bot._handle_update(_msg("hello?"))
    assert "inline_keyboard" in (api.sent[-1]["reply_markup"] or {})


def test_a_pasted_market_url_opens_that_market(bot_and_api):
    # Pasting a link from polymarket.com is unambiguous intent.
    bot, api = bot_and_api
    url = "https://polymarket.com/event/ev/will-thing-0-happen"
    briefing = {"ok": True, "text": "brief", "url": url}
    with mock.patch.object(bot_module.service, "briefing", return_value=briefing) as spy:
        bot._handle_update(_msg(url))
    assert spy.call_args.args[0] == url


def test_a_random_sentence_does_not_get_looked_up_as_a_market(bot_and_api):
    # Guessing wrong sends the owner to an error instead of the menu.
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "briefing", side_effect=AssertionError("looked up")), \
         mock.patch.object(bot_module.service, "status", return_value={"ok": True, "portfolio": {}}):
        bot._handle_update(_msg("what is going on here"))
