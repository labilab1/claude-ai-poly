"""Regression tests for menu state leaking between screens.

Every test here corresponds to a defect a user actually hit: the menu felt
like it "broke" after a search, and the button row gave no way to tell which
market a button belonged to. All of these are silent - the bot answers
confidently with the wrong screen.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot.config import Settings
from polymarket_bot.telegram import bot as bot_module
from polymarket_bot.telegram import menu as menu_mod
from polymarket_bot.telegram.bot import TelegramBot
from polymarket_bot.telegram.i18n import t

OWNER = 12345


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.acks: list[dict] = []
        self._next_id = 100

    def get_me(self):
        return {"username": "test_bot"}

    def get_updates(self, *, offset=None, timeout=25):
        return []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": self._next_id}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.edits.append({"text": text})
        return {}

    def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        self.acks.append({"text": text})
        return {}

    def all_text(self) -> str:
        return "\n".join(s["text"] for s in self.sent)

    def markups(self) -> list:
        return [s["reply_markup"] for s in self.sent if s["reply_markup"]]

    def last_inline(self) -> list:
        for entry in reversed(self.sent):
            markup = entry.get("reply_markup") or {}
            if "inline_keyboard" in markup:
                return markup["inline_keyboard"]
        return []

    def has_reply_keyboard(self) -> bool:
        return any("keyboard" in (m or {}) for m in self.markups())


@pytest.fixture
def bot_and_api(tmp_path):
    api = FakeAPI()
    settings = Settings(
        private_key="0x0", wallet="0xw", data_dir=tmp_path,
        telegram_bot_token="tok", telegram_chat_id=str(OWNER),
    )
    return TelegramBot(settings, client=SimpleNamespace(), api=api), api


def _msg(text, chat_id=OWNER):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def _cb(data, chat_id=OWNER):
    return {"callback_query": {"id": "cb", "data": data,
                               "message": {"chat": {"id": chat_id}, "message_id": 1}}}


def _scan(count=6):
    return {
        "ok": True, "sort": "hot", "text": "raw",
        "markets": [
            {
                "slug": f"market-{i}", "condition_id": f"0x{i}",
                "question": f"Will thing {i} happen and also have quite a long title?",
                "yes": {"label": "Yes", "price": 0.6}, "spread": 0.012,
                "days_left": 14, "volume_24h": 1_000_000 - i,
                "url": f"https://polymarket.com/event/ev/market-{i}",
            }
            for i in range(count)
        ],
    }


# ---------------------------------------------------------------------------
# state leaking between Hot and Search
# ---------------------------------------------------------------------------


def test_tapping_hot_after_a_search_shows_hot_not_the_old_search(bot_and_api):
    """The bug: session.query survived, so Hot silently returned the previous
    search - titled "Results for <old query>" - and the menu looked broken."""
    bot, api = bot_and_api
    bot._handle_update(_msg(t("menu.search", "en")))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_msg("ethiopia"))

    with mock.patch.object(bot_module.service, "scan", return_value=_scan()) as spy:
        bot._handle_update(_msg(t("menu.hot", "en")))

    assert spy.call_args.kwargs["keyword"] is None, "Hot was filtered by the old search"
    assert "ethiopia" not in api.sent[-1]["text"].lower()


def test_tapping_search_again_asks_for_a_new_keyword(bot_and_api):
    """The bug: with a query already set, Search re-rendered the old results
    instead of prompting, so a second search was impossible."""
    bot, api = bot_and_api
    bot._handle_update(_msg(t("menu.search", "en")))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_msg("ethiopia"))

    bot._handle_update(_msg(t("menu.search", "en")))

    assert bot._sessions.get(OWNER).awaiting == menu_mod.AWAIT_SEARCH
    assert "keyword" in api.sent[-1]["text"].lower()


def test_a_second_search_replaces_the_first(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_msg(t("menu.search", "en")))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_msg("ethiopia"))
    bot._handle_update(_msg(t("menu.search", "en")))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()) as spy:
        bot._handle_update(_msg("bitcoin"))
    assert spy.call_args.kwargs["keyword"] == "bitcoin"


def test_paging_a_search_keeps_the_query(bot_and_api):
    # The query must survive pagination even though it is cleared by Hot.
    bot, api = bot_and_api
    bot._handle_update(_msg(t("menu.search", "en")))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan(count=12)):
        bot._handle_update(_msg("ethiopia"))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan(count=12)) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SEARCH}:1"))
    assert spy.call_args.kwargs["keyword"] == "ethiopia"


def test_going_back_from_a_market_returns_to_the_list_it_came_from(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_msg(t("menu.search", "en")))
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_msg("ethiopia"))
    token = _first_market_token(api)

    briefing = {"ok": True, "text": "brief", "url": "https://polymarket.com/x"}
    with mock.patch.object(bot_module.service, "briefing", return_value=briefing):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MARKET}:{token}"))

    back = [b for row in api.last_inline() for b in row
            if "Back" in b.get("text", "")][0]["callback_data"]
    assert back.startswith(f"nav:{menu_mod.VIEW_SEARCH}:")


# ---------------------------------------------------------------------------
# the persistent keyboard must survive a deleted chat / restarted bot
# ---------------------------------------------------------------------------


def test_the_first_screen_after_a_restart_carries_the_reply_keyboard(bot_and_api):
    """Deleting the chat clears the reply keyboard on Telegram's side. The
    first screen rendered afterwards has to put it back, or the menu is simply
    gone and the only way back is a typed command."""
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    assert api.has_reply_keyboard(), "a list screen left the user with no menu"


def test_the_keyboard_is_not_resent_on_every_screen(bot_and_api):
    # Re-sending it constantly makes the chat flicker and adds a message per
    # tap; once per session is the point.
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
        first = sum(1 for m in api.markups() if "keyboard" in m)
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
        second = sum(1 for m in api.markups() if "keyboard" in m)
    assert second == first, "the reply keyboard was re-sent on a repeat screen"


def test_switching_language_repaints_the_keyboard(bot_and_api):
    # The labels changed, so the old keyboard is now wrong.
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    before = sum(1 for m in api.markups() if "keyboard" in m)
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_LANG}:"))
    after = sum(1 for m in api.markups() if "keyboard" in m)
    assert after > before, "the keyboard kept the old language's labels"


# ---------------------------------------------------------------------------
# button clarity: which button belongs to which market
# ---------------------------------------------------------------------------


def _first_market_token(api) -> str:
    for row in api.last_inline():
        for button in row:
            data = button.get("callback_data", "")
            if data.startswith(f"nav:{menu_mod.VIEW_MARKET}:"):
                return data.split(":")[-1]
    raise AssertionError("no market button found")


def test_each_market_button_names_the_market_it_opens(bot_and_api):
    """The bug the user reported: five identical "Details" buttons in a column
    with nothing tying a button to a row."""
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))

    labels = [
        b["text"] for row in api.last_inline() for b in row
        if b.get("callback_data", "").startswith(f"nav:{menu_mod.VIEW_MARKET}:")
    ]
    assert len(labels) == menu_mod.PAGE_SIZE
    assert len(set(labels)) == len(labels), f"market buttons are not distinguishable: {labels}"
    # Each carries its row number, so the button maps onto the numbered text.
    for number, label in enumerate(labels, start=1):
        assert label.startswith(f"{number}"), f"button {label!r} does not lead with its row number"


def test_market_buttons_stay_within_telegrams_practical_label_width(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    for row in api.last_inline():
        for button in row:
            assert len(button["text"]) <= 40, f"button label too long: {button['text']!r}"


def test_the_detail_screen_buy_buttons_state_the_price_they_would_pay(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    token = _first_market_token(api)

    briefing = {
        "ok": True, "text": "brief", "url": "https://polymarket.com/x",
        "yes_price": 0.44, "no_price": 0.56,
    }
    with mock.patch.object(bot_module.service, "briefing", return_value=briefing):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MARKET}:{token}"))

    buy_labels = [
        b["text"] for row in api.last_inline() for b in row
        if b.get("callback_data", "").startswith(f"nav:{menu_mod.VIEW_BUY}:")
    ]
    assert any("44" in label for label in buy_labels), f"no YES price on the buy button: {buy_labels}"
    assert any("56" in label for label in buy_labels), f"no NO price on the buy button: {buy_labels}"


def test_a_stale_market_token_recovers_to_a_list_instead_of_dead_ending(bot_and_api):
    """After a restart every token from the old process is unknown. Saying
    "expired" and stopping leaves the user with nothing to tap."""
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "scan", return_value=_scan()) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MARKET}:deadbeef"))
    assert spy.called, "a stale token did not recover to a fresh list"
    assert api.last_inline(), "no buttons offered after a stale token"
