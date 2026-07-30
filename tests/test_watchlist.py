"""Tests for the watchlist store and its menu flow.

A watchlist entry is a bookmark and nothing else. The most important property
is what it CANNOT do: it holds no size, no price and no instruction, so no
amount of corruption in this file can cause a trade.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot.config import Settings
from polymarket_bot.telegram import bot as bot_module
from polymarket_bot.telegram import menu as menu_mod
from polymarket_bot.telegram.bot import TelegramBot
from polymarket_bot.telegram.watchlist import MAX_ENTRIES, Watchlist

OWNER = 12345


def _settings(tmp: Path) -> Settings:
    return Settings(
        private_key="0x0", wallet="0xw", data_dir=tmp,
        telegram_bot_token="tok", telegram_chat_id=str(OWNER),
    )


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_a_new_watchlist_is_empty(tmp_path):
    assert Watchlist(_settings(tmp_path)).list(OWNER) == []


def test_toggle_adds_then_removes(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    assert wl.toggle(OWNER, "market-a") is True
    assert wl.list(OWNER) == ["market-a"]
    assert wl.toggle(OWNER, "market-a") is False
    assert wl.list(OWNER) == []


def test_entries_survive_a_reload(tmp_path):
    # The whole point of persisting: a bookmark that vanished on restart is
    # not a bookmark.
    settings = _settings(tmp_path)
    Watchlist(settings).toggle(OWNER, "market-a")
    assert Watchlist(settings).list(OWNER) == ["market-a"]


def test_newest_entries_come_first(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    wl.toggle(OWNER, "first")
    wl.toggle(OWNER, "second")
    assert wl.list(OWNER) == ["second", "first"]


def test_the_list_is_capped(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    for i in range(MAX_ENTRIES + 10):
        wl.toggle(OWNER, f"m{i}")
    assert len(wl.list(OWNER)) == MAX_ENTRIES


def test_the_oldest_entry_falls_off_when_capped(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    for i in range(MAX_ENTRIES + 1):
        wl.toggle(OWNER, f"m{i}")
    assert "m0" not in wl.list(OWNER)
    assert f"m{MAX_ENTRIES}" in wl.list(OWNER)


def test_chats_do_not_share_a_watchlist(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    wl.toggle(111, "a")
    wl.toggle(222, "b")
    assert wl.list(111) == ["a"] and wl.list(222) == ["b"]


def test_writing_one_chat_preserves_another(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    wl.toggle(111, "a")
    wl.toggle(222, "b")
    wl.toggle(111, "c")
    assert wl.list(222) == ["b"]


def test_contains_reports_membership(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    wl.toggle(OWNER, "a")
    assert wl.contains(OWNER, "a") and not wl.contains(OWNER, "b")


def test_clear_empties_one_chat_only(tmp_path):
    wl = Watchlist(_settings(tmp_path))
    wl.toggle(111, "a")
    wl.toggle(222, "b")
    wl.clear(111)
    assert wl.list(111) == [] and wl.list(222) == ["b"]


# ---------------------------------------------------------------------------
# a corrupt file must never take down a screen
# ---------------------------------------------------------------------------


def test_a_corrupt_file_reads_as_empty(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    (tmp_path / "telegram_watchlist.json").write_text("{not json", encoding="utf-8")
    assert Watchlist(settings).list(OWNER) == []


def test_a_non_dict_payload_reads_as_empty(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    (tmp_path / "telegram_watchlist.json").write_text("[1,2,3]", encoding="utf-8")
    assert Watchlist(settings).list(OWNER) == []


def test_non_string_entries_are_dropped_rather_than_trusted(tmp_path):
    # The file is hand-editable, so it is coerced rather than believed.
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    (tmp_path / "telegram_watchlist.json").write_text(
        '{"12345": ["good", 42, null, {"x": 1}, ""]}', encoding="utf-8"
    )
    assert Watchlist(settings).list(OWNER) == ["good"]


def test_a_missing_directory_is_created_on_write(tmp_path):
    settings = Settings(private_key="0x0", wallet="0xw", data_dir=tmp_path / "nested" / "deep")
    Watchlist(settings).toggle(OWNER, "a")
    assert Watchlist(settings).list(OWNER) == ["a"]


# ---------------------------------------------------------------------------
# menu flow
# ---------------------------------------------------------------------------


class FakeAPI:
    def __init__(self):
        self.sent: list[dict] = []

    def get_me(self):
        return {"username": "b"}

    def get_updates(self, *, offset=None, timeout=25):
        return []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def edit_message_text(self, *a, **k):
        return {}

    def answer_callback_query(self, *a, **k):
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
    return TelegramBot(_settings(tmp_path), client=SimpleNamespace(), api=api), api


def _cb(data):
    return {"callback_query": {"id": "cb", "data": data,
                               "message": {"chat": {"id": OWNER}, "message_id": 1}}}


_SCAN = {
    "ok": True, "sort": "hot", "text": "raw",
    "markets": [{
        "slug": "market-0", "condition_id": "0x0", "question": "Will it happen?",
        "yes": {"price": 0.6}, "spread": 0.01, "days_left": 3, "volume_24h": 1000,
        "url": "https://polymarket.com/event/e/market-0",
    }],
}
_BRIEF = {
    "ok": True, "text": "brief", "url": "https://polymarket.com/event/e/market-0",
    "question": "Will it happen?", "yes_price": 0.6, "no_price": 0.4, "book": {"spread": 0.01},
}


def _open_market(bot, api):
    with mock.patch.object(bot_module.service, "scan", return_value=_SCAN):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_HOT}:0"))
    for row in api.last_inline():
        for button in row:
            data = button.get("callback_data", "")
            if data.startswith(f"nav:{menu_mod.VIEW_MARKET}:"):
                return data.split(":")[-1]
    raise AssertionError("no market button")


def test_watching_a_market_from_its_detail_screen(bot_and_api):
    bot, api = bot_and_api
    token = _open_market(bot, api)
    with mock.patch.object(bot_module.service, "briefing", return_value=_BRIEF):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MARKET}:{token}"))
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_WATCH}:{token}"))
    assert bot._watchlist.list(OWNER) == ["market-0"]
    assert "watchlist" in api.all_text().lower()


def test_the_detail_button_flips_to_stop_watching(bot_and_api):
    bot, api = bot_and_api
    token = _open_market(bot, api)
    with mock.patch.object(bot_module.service, "briefing", return_value=_BRIEF):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_WATCH}:{token}"))
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_MARKET}:{token}"))
    labels = [b["text"] for row in api.last_inline() for b in row]
    assert any("Stop watching" in label for label in labels)


def test_an_empty_watchlist_says_how_to_fill_it(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_WATCHLIST}:"))
    assert "empty" in api.all_text().lower()


def test_the_watchlist_screen_lists_watched_markets(bot_and_api):
    bot, api = bot_and_api
    bot._watchlist.toggle(OWNER, "market-0")
    with mock.patch.object(bot_module.service, "briefing", return_value=_BRIEF):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_WATCHLIST}:"))
    assert "Will it happen?" in api.all_text()


def test_one_unreadable_bookmark_does_not_blank_the_screen(bot_and_api):
    bot, api = bot_and_api
    bot._watchlist.toggle(OWNER, "good")
    bot._watchlist.toggle(OWNER, "broken")

    def _briefing(ref, **kwargs):
        if ref == "broken":
            return {"ok": False, "error": "market gone"}
        return _BRIEF

    with mock.patch.object(bot_module.service, "briefing", side_effect=_briefing):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_WATCHLIST}:"))
    text = api.all_text()
    assert "market gone" in text
    assert "Will it happen?" in text, "a broken entry hid the working ones"


def test_watching_with_a_stale_token_does_not_write_garbage(bot_and_api):
    bot, api = bot_and_api
    bot._handle_update(_cb(f"nav:{menu_mod.VIEW_WATCH}:deadbeef"))
    assert bot._watchlist.list(OWNER) == []
