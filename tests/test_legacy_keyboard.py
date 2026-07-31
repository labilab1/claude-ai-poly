"""Regression tests for removing the reply keyboard left by the old menu.

The defect, reported with a screenshot: after the menu was rebuilt as inline
buttons, the four buttons from the previous version were STILL pinned above
the input box, and tapping one posted its label into the chat as a message.

Deleting the code that sent the keyboard was not enough. Telegram keeps a
reply keyboard on the client until a message explicitly removes it, and the
bot has no way to observe that it is still there.
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
    def __init__(self):
        self.sent: list[dict] = []
        self.deleted: list[int] = []
        self.screens: list[dict] = []
        self._next = 100

    def get_me(self):
        return {"username": "b"}

    def get_updates(self, *, offset=None, timeout=25):
        return []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self._next += 1
        entry = {"text": text, "reply_markup": reply_markup, "message_id": self._next}
        self.sent.append(entry)
        self.screens.append(entry)
        return {"message_id": self._next}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.screens.append({"text": text, "reply_markup": reply_markup})
        return {}

    def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)
        return {}

    def answer_callback_query(self, *a, **k):
        return {}

    def removals(self) -> list[dict]:
        return [s for s in self.sent if (s["reply_markup"] or {}).get("remove_keyboard")]

    def all_text(self) -> str:
        return "\n".join(s["text"] for s in self.screens)


@pytest.fixture
def bot_and_api(tmp_path):
    api = FakeAPI()
    settings = Settings(
        private_key="0x0", wallet="0xWALLET", data_dir=tmp_path,
        telegram_bot_token="tok", telegram_chat_id=str(OWNER),
    )
    return TelegramBot(settings, client=SimpleNamespace(), api=api), api


def _msg(text):
    return {"message": {"chat": {"id": OWNER}, "text": text}}


_STATUS = {"ok": True, "portfolio": {"cash_usdc": 21.6, "total_value": 21.6, "open_positions": 0}}


def _with_status():
    return mock.patch.object(bot_module.service, "status", return_value=_STATUS)


# ---------------------------------------------------------------------------
# the removal itself
# ---------------------------------------------------------------------------


def test_start_removes_the_old_reply_keyboard(bot_and_api):
    bot, api = bot_and_api
    with _with_status():
        bot._handle_update(_msg("/start"))
    assert api.removals(), "the leftover keyboard was never removed"


def test_the_removal_message_is_deleted_so_it_leaves_no_clutter(bot_and_api):
    """A markup can only ride on a message, so the removal needs a throwaway.
    Leaving it behind puts a stray message in the chat on every restart."""
    bot, api = bot_and_api
    with _with_status():
        bot._handle_update(_msg("/start"))
    removal_id = api.removals()[0]["message_id"]
    assert removal_id in api.deleted


def test_the_removal_happens_only_once_per_session(bot_and_api):
    bot, api = bot_and_api
    with _with_status():
        bot._handle_update(_msg("/start"))
        bot._handle_update(_msg("/start"))
    assert len(api.removals()) == 1, "the keyboard removal repeated on every screen"


def test_a_failure_to_remove_does_not_break_the_screen(bot_and_api):
    # A stale keyboard is cosmetic; failing the dashboard over it is not.
    bot, api = bot_and_api
    api.delete_message = mock.Mock(side_effect=RuntimeError("cannot delete"))
    with _with_status():
        bot._handle_update(_msg("/start"))
    assert "Polymarket" in api.all_text()


# ---------------------------------------------------------------------------
# a tap on the stale keyboard, before it is removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label_key,view", [
    ("menu.hot", menu_mod.VIEW_HOT),
    ("menu.search", menu_mod.VIEW_SEARCH),
    ("menu.portfolio", menu_mod.VIEW_PORTFOLIO),
    ("menu.more", menu_mod.VIEW_MORE),
])
@pytest.mark.parametrize("lang", ["en", "he"])
def test_a_stale_keyboard_tap_still_navigates(bot_and_api, label_key, view, lang):
    """The tap arrives as plain text. Until the keyboard is gone it must still
    go where the button said, not dump the owner on the home screen."""
    bot, api = bot_and_api
    scan = {"ok": True, "markets": [], "text": ""}
    with _with_status(), \
         mock.patch.object(bot_module.service, "scan", return_value=scan), \
         mock.patch.object(bot_module.service, "positions", return_value={"ok": True, "text": "none"}):
        bot._handle_update(_msg(t(label_key, lang)))
    assert bot._sessions.get(OWNER).view == view


def test_a_stale_keyboard_tap_also_removes_the_keyboard(bot_and_api):
    bot, api = bot_and_api
    scan = {"ok": True, "markets": [], "text": ""}
    with mock.patch.object(bot_module.service, "scan", return_value=scan):
        bot._handle_update(_msg(t("menu.hot", "en")))
    assert api.removals(), "tapping the stale keyboard did not clear it"


def test_the_labels_are_recognised_in_both_languages(bot_and_api):
    # The keyboard on screen may be in the language used when it was painted.
    labels = menu_mod.legacy_reply_labels()
    for lang in ("en", "he"):
        assert labels[t("menu.hot", lang)] == menu_mod.VIEW_HOT
        assert labels[t("menu.more", lang)] == menu_mod.VIEW_MORE


def test_ordinary_text_is_not_mistaken_for_a_stale_button(bot_and_api):
    bot, api = bot_and_api
    with _with_status():
        bot._handle_update(_msg("just some words"))
    assert bot._sessions.get(OWNER).view == menu_mod.VIEW_HOME


# ---------------------------------------------------------------------------
# the dashboard itself
# ---------------------------------------------------------------------------


def test_the_dashboard_shows_the_deposit_wallet(bot_and_api):
    # Public address, and the one value people check against the website.
    bot, api = bot_and_api
    with _with_status():
        bot._handle_update(_msg("/start"))
    assert "0xWALLET" in api.all_text()


def test_the_dashboard_never_shows_the_private_key(bot_and_api):
    bot, api = bot_and_api
    with _with_status():
        bot._handle_update(_msg("/start"))
    assert "0x0" != api.all_text().strip()
    assert bot.settings.private_key not in api.all_text()


def test_every_home_button_is_inline(bot_and_api):
    bot, api = bot_and_api
    with _with_status():
        bot._handle_update(_msg("/start"))
    dashboard = [s for s in api.sent if "Polymarket" in s["text"]][-1]
    markup = dashboard["reply_markup"] or {}
    assert "inline_keyboard" in markup
    assert "keyboard" not in markup, "a reply keyboard would post text again"
