"""Offline tests for the Telegram command router and confirm/cancel flow.

No real network calls and no real SecureClient: `service.*` functions are
monkeypatched, and a FakeAPI test double stands in for Telegram's HTTP API.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot.config import Settings
from polymarket_bot.telegram import bot as bot_module
from polymarket_bot.telegram.bot import TelegramBot

OWNER_CHAT_ID = 12345
STRANGER_CHAT_ID = 999


class FakeAPI:
    """Records every call instead of talking to Telegram."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.acks: list[dict] = []
        self._next_message_id = 100
        self.get_updates_calls = 0
        self._get_updates_queue: list[Exception | list] = []

    def queue_updates(self, item) -> None:
        self._get_updates_queue.append(item)

    def get_me(self) -> dict:
        return {"username": "test_bot"}

    def get_updates(self, *, offset=None, timeout=25) -> list:
        self.get_updates_calls += 1
        if self._get_updates_queue:
            item = self._get_updates_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return []

    def send_message(self, chat_id, text, *, reply_markup=None) -> dict:
        self._next_message_id += 1
        self.sent.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "message_id": self._next_message_id}
        )
        return {"message_id": self._next_message_id}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None) -> dict:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
        return {}

    def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False) -> dict:
        self.acks.append({"id": callback_query_id, "text": text})
        return {}


def _settings(chat_id: str | None = str(OWNER_CHAT_ID)) -> Settings:
    return Settings(
        private_key="0x0", wallet="0xw",
        telegram_bot_token="test-token", telegram_chat_id=chat_id,
    )


def _make_bot(chat_id: str | None = str(OWNER_CHAT_ID)) -> tuple[TelegramBot, FakeAPI]:
    api = FakeAPI()
    client_sentinel = SimpleNamespace(name="the-one-persistent-client")
    bot = TelegramBot(_settings(chat_id), client=client_sentinel, api=api)
    return bot, api


def _message(chat_id: int, text: str) -> dict:
    return {"chat": {"id": chat_id}, "text": text}


def _callback(chat_id: int, message_id: int, data: str, callback_id: str = "cb1") -> dict:
    return {"id": callback_id, "data": data, "message": {"chat": {"id": chat_id}, "message_id": message_id}}


# --------------------------------------------------------------------------
# access control
# --------------------------------------------------------------------------


def test_bootstrap_mode_reveals_chat_id_and_touches_nothing_else():
    bot, api = _make_bot(chat_id=None)
    with mock.patch.object(bot_module.service, "status", side_effect=AssertionError("must not be called")):
        bot._handle_command(_message(STRANGER_CHAT_ID, "/status"), "/status")

    assert len(api.sent) == 1
    assert str(STRANGER_CHAT_ID) in api.sent[0]["text"]
    assert f"TELEGRAM_CHAT_ID={STRANGER_CHAT_ID}" in api.sent[0]["text"]


def test_unauthorized_chat_is_silently_ignored():
    bot, api = _make_bot()
    with mock.patch.object(bot_module.service, "status", side_effect=AssertionError("must not be called")):
        bot._handle_command(_message(STRANGER_CHAT_ID, "/status"), "/status")
    assert api.sent == []


def test_unauthorized_callback_is_acked_but_otherwise_ignored():
    bot, api = _make_bot()
    bot._pending["tok1"] = bot_module._Pending(
        action="buy", market_ref="m", outcome="yes", confirm_args={},
        chat_id=STRANGER_CHAT_ID, message_id=1, created_at=time.monotonic(),
    )
    bot._handle_callback(_callback(STRANGER_CHAT_ID, 1, "confirm:tok1"))
    assert api.edits == []
    assert "tok1" in bot._pending  # untouched - the pending belongs to nobody who can act on it
    assert len(api.acks) == 1


# --------------------------------------------------------------------------
# informational commands
# --------------------------------------------------------------------------


def test_help_never_touches_service():
    bot, api = _make_bot()
    with mock.patch.object(bot_module.service, "status", side_effect=AssertionError("must not be called")):
        for cmd in ("/start", "/help"):
            bot._handle_command(_message(OWNER_CHAT_ID, cmd), cmd)
    assert any("/buy" in m["text"] for m in api.sent)


def test_status_calls_service_with_the_persistent_client():
    bot, api = _make_bot()
    fake_status = mock.Mock(return_value={"ok": True, "text": "ACCOUNT: $21.83"})
    with mock.patch.object(bot_module.service, "status", fake_status):
        bot._handle_command(_message(OWNER_CHAT_ID, "/status"), "/status")

    fake_status.assert_called_once_with(client=bot.client)
    assert api.sent[-1]["text"] == "ACCOUNT: $21.83"


def test_positions_all_flag_includes_resolved():
    bot, api = _make_bot()
    fake = mock.Mock(return_value={"ok": True, "text": "..."})
    with mock.patch.object(bot_module.service, "positions", fake):
        bot._handle_command(_message(OWNER_CHAT_ID, "/positions all"), "/positions all")
    fake.assert_called_once_with(include_resolved=True, client=bot.client)


def test_scan_joins_keyword_args():
    bot, api = _make_bot()
    fake = mock.Mock(return_value={"ok": True, "text": "..."})
    with mock.patch.object(bot_module.service, "scan", fake):
        bot._handle_command(_message(OWNER_CHAT_ID, "/scan will invade"), "/scan will invade")
    fake.assert_called_once_with(limit=15, keyword="will invade", client=bot.client)


# --------------------------------------------------------------------------
# buy / sell preview -> confirm keyboard
# --------------------------------------------------------------------------


def test_buy_preview_with_confirm_args_attaches_keyboard_and_stores_pending():
    bot, api = _make_bot()
    confirm_args = {"usd": 1.0, "expected_usdc": 1.0, "limit_price": None, "confirm": True}
    fake_buy = mock.Mock(
        return_value={
            "ok": False, "needs_confirmation": True, "confirm_args": confirm_args,
            "text": "ORDER PLAN - MARKET BUY\n  Spend: $1.00",
        }
    )
    with mock.patch.object(bot_module.service, "buy", fake_buy):
        bot._handle_command(_message(OWNER_CHAT_ID, "/buy iran-nuke yes 1"), "/buy iran-nuke yes 1")

    fake_buy.assert_called_once_with(
        "iran-nuke", "yes", 1.0, limit_price=None, confirm=False, client=bot.client
    )
    assert len(api.sent) == 1
    sent = api.sent[0]
    assert sent["reply_markup"] is not None
    row = sent["reply_markup"]["inline_keyboard"][0]
    assert row[0]["callback_data"].startswith("confirm:")
    assert row[1]["callback_data"].startswith("cancel:")
    assert len(bot._pending) == 1
    pending = next(iter(bot._pending.values()))
    assert pending.action == "buy"
    assert pending.market_ref == "iran-nuke"
    assert pending.outcome == "yes"
    assert pending.confirm_args == confirm_args


def test_buy_refused_shows_no_keyboard_and_stores_nothing():
    bot, api = _make_bot()
    fake_buy = mock.Mock(
        return_value={"ok": False, "needs_confirmation": False, "text": "REFUSED - over cap. NOTHING SENT."}
    )
    with mock.patch.object(bot_module.service, "buy", fake_buy):
        bot._handle_command(_message(OWNER_CHAT_ID, "/buy m yes 999"), "/buy m yes 999")

    assert api.sent[0]["reply_markup"] is None
    assert bot._pending == {}


def test_sell_all_maps_to_fraction_one():
    bot, api = _make_bot()
    fake_sell = mock.Mock(return_value={"ok": False, "needs_confirmation": False, "text": "n/a"})
    with mock.patch.object(bot_module.service, "sell", fake_sell):
        bot._handle_command(_message(OWNER_CHAT_ID, "/sell m yes all"), "/sell m yes all")
    fake_sell.assert_called_once_with(
        "m", "yes", shares=None, fraction=1.0, limit_price=None, confirm=False, client=bot.client
    )


def test_sell_numeric_size_maps_to_shares():
    bot, api = _make_bot()
    fake_sell = mock.Mock(return_value={"ok": False, "needs_confirmation": False, "text": "n/a"})
    with mock.patch.object(bot_module.service, "sell", fake_sell):
        bot._handle_command(_message(OWNER_CHAT_ID, "/sell m yes 3.5"), "/sell m yes 3.5")
    fake_sell.assert_called_once_with(
        "m", "yes", shares=3.5, fraction=None, limit_price=None, confirm=False, client=bot.client
    )


# --------------------------------------------------------------------------
# confirm / cancel callback
# --------------------------------------------------------------------------


def test_confirm_executes_with_the_exact_confirm_args_and_edits_the_message():
    bot, api = _make_bot()
    confirm_args = {"usd": 1.0, "expected_usdc": 1.0, "limit_price": None, "confirm": True}
    token = "tok-abc"
    bot._pending[token] = bot_module._Pending(
        action="buy", market_ref="iran-nuke", outcome="yes", confirm_args=confirm_args,
        chat_id=OWNER_CHAT_ID, message_id=555, created_at=time.monotonic(),
    )
    fake_buy = mock.Mock(return_value={"ok": True, "text": "BUY filled 19.23 shares for $1.00"})
    with mock.patch.object(bot_module.service, "buy", fake_buy):
        bot._handle_callback(_callback(OWNER_CHAT_ID, 555, f"confirm:{token}"))

    fake_buy.assert_called_once_with("iran-nuke", "yes", **confirm_args, client=bot.client)
    assert token not in bot._pending  # consumed exactly once
    assert len(api.edits) == 1
    assert "BUY filled" in api.edits[0]["text"]
    assert api.edits[0]["chat_id"] == OWNER_CHAT_ID
    assert api.edits[0]["message_id"] == 555


def test_confirm_is_one_shot_a_replayed_tap_is_rejected():
    bot, api = _make_bot()
    token = "tok-once"
    bot._pending[token] = bot_module._Pending(
        action="buy", market_ref="m", outcome="yes", confirm_args={"usd": 1.0, "confirm": True},
        chat_id=OWNER_CHAT_ID, message_id=1, created_at=time.monotonic(),
    )
    fake_buy = mock.Mock(return_value={"ok": True, "text": "filled"})
    with mock.patch.object(bot_module.service, "buy", fake_buy):
        bot._handle_callback(_callback(OWNER_CHAT_ID, 1, f"confirm:{token}"))
        bot._handle_callback(_callback(OWNER_CHAT_ID, 1, f"confirm:{token}", callback_id="cb2"))

    assert fake_buy.call_count == 1  # the replay never reaches service.buy
    assert "no longer valid" in api.edits[-1]["text"]


def test_cancel_discards_pending_and_sends_no_order():
    bot, api = _make_bot()
    token = "tok-cancel"
    bot._pending[token] = bot_module._Pending(
        action="sell", market_ref="m", outcome="no", confirm_args={"fraction": 1.0, "confirm": True},
        chat_id=OWNER_CHAT_ID, message_id=7, created_at=time.monotonic(),
    )
    with mock.patch.object(bot_module.service, "sell", side_effect=AssertionError("must not be called")):
        bot._handle_callback(_callback(OWNER_CHAT_ID, 7, f"cancel:{token}"))

    assert token not in bot._pending
    assert api.edits[-1]["text"] == "Cancelled - nothing was sent."


def test_expired_confirmation_is_refused_without_sending():
    bot, api = _make_bot()
    token = "tok-old"
    bot._pending[token] = bot_module._Pending(
        action="buy", market_ref="m", outcome="yes", confirm_args={"usd": 1.0, "confirm": True},
        chat_id=OWNER_CHAT_ID, message_id=1,
        created_at=time.monotonic() - bot_module._PENDING_TTL_SECONDS - 5,
    )
    with mock.patch.object(bot_module.service, "buy", side_effect=AssertionError("must not be called")):
        bot._handle_callback(_callback(OWNER_CHAT_ID, 1, f"confirm:{token}"))
    assert "expired" in api.edits[-1]["text"].lower()
    assert token not in bot._pending


def test_unknown_token_is_reported_as_invalid():
    bot, api = _make_bot()
    with mock.patch.object(bot_module.service, "buy", side_effect=AssertionError("must not be called")):
        bot._handle_callback(_callback(OWNER_CHAT_ID, 1, "confirm:does-not-exist"))
    assert "no longer valid" in api.edits[-1]["text"]


def test_confirm_survives_service_raising():
    """A bug in the confirm path must edit an error message, not crash the bot."""
    bot, api = _make_bot()
    token = "tok-boom"
    bot._pending[token] = bot_module._Pending(
        action="buy", market_ref="m", outcome="yes", confirm_args={"usd": 1.0, "confirm": True},
        chat_id=OWNER_CHAT_ID, message_id=1, created_at=time.monotonic(),
    )
    with mock.patch.object(bot_module.service, "buy", side_effect=RuntimeError("boom")):
        bot._handle_callback(_callback(OWNER_CHAT_ID, 1, f"confirm:{token}"))
    assert "boom" in api.edits[-1]["text"]


# --------------------------------------------------------------------------
# callback namespaces
#
# The trade flow owns `confirm:` / `cancel:` only. Every other prefix belongs
# to a different feature (menu navigation), and routing it into the pending
# trade lookup answers "this button expired" for a button that never was a
# trade in the first place.
# --------------------------------------------------------------------------


def test_a_non_trade_callback_does_not_consume_the_pending_trade_lookup():
    bot, api = _make_bot()
    token = "tok-live"
    bot._pending[token] = bot_module._Pending(
        action="buy", market_ref="m", outcome="yes", confirm_args={"usd": 1.0, "confirm": True},
        chat_id=OWNER_CHAT_ID, message_id=1, created_at=time.monotonic(),
    )

    # A menu tap arrives while a trade confirmation is outstanding.
    bot._handle_callback(_callback(OWNER_CHAT_ID, 2, "nav:hot:0"))

    assert token in bot._pending, "a menu tap consumed the outstanding trade confirmation"
    # The visible symptom: the menu tap gets answered as a dead trade button.
    said = " ".join(
        [e["text"] for e in api.edits] + [a["text"] or "" for a in api.acks]
    ).lower()
    assert "no longer valid" not in said and "expired" not in said, (
        f"a menu tap was answered as a stale trade confirmation: {said!r}"
    )


def test_an_unknown_callback_prefix_is_rejected_without_touching_pending():
    bot, api = _make_bot()
    token = "tok-live"
    bot._pending[token] = bot_module._Pending(
        action="buy", market_ref="m", outcome="yes", confirm_args={"usd": 1.0, "confirm": True},
        chat_id=OWNER_CHAT_ID, message_id=1, created_at=time.monotonic(),
    )

    bot._handle_callback(_callback(OWNER_CHAT_ID, 2, "bogus:whatever"))

    assert token in bot._pending
    assert any("nrecognized" in (a["text"] or "") for a in api.acks)


def test_an_unauthorized_menu_callback_is_ignored():
    # The single-owner lock has to hold on every namespace, not just trades.
    bot, api = _make_bot()
    bot._handle_callback(_callback(STRANGER_CHAT_ID, 1, "nav:hot:0"))
    assert api.sent == [] and api.edits == []


# --------------------------------------------------------------------------
# /rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,value,expected_kw",
    [
        ("pct", "-25", {"target_pct": -25.0}),
        ("price", "0.30", {"target_price": 0.30}),
        ("trail", "20", {"trail_pct": 20.0}),
    ],
)
def test_rule_mode_maps_to_the_right_keyword(mode, value, expected_kw):
    bot, api = _make_bot()
    fake_set_rule = mock.Mock(return_value={"ok": True, "text": "RULE SET"})
    with mock.patch.object(bot_module.service, "set_rule", fake_set_rule):
        bot._handle_command(
            _message(OWNER_CHAT_ID, f"/rule m yes stop_loss {mode} {value}"),
            f"/rule m yes stop_loss {mode} {value}",
        )
    fake_set_rule.assert_called_once_with("m", "yes", "stop_loss", client=bot.client, **expected_kw)


def test_rule_unknown_mode_does_not_call_service():
    bot, api = _make_bot()
    with mock.patch.object(bot_module.service, "set_rule", side_effect=AssertionError("must not be called")):
        bot._handle_command(_message(OWNER_CHAT_ID, "/rule m yes stop_loss bogus 5"), "/rule m yes stop_loss bogus 5")
    assert "Unknown mode" in api.sent[-1]["text"]


# --------------------------------------------------------------------------
# resilience
# --------------------------------------------------------------------------


def test_one_bad_command_does_not_crash_the_router():
    bot, api = _make_bot()
    with mock.patch.object(bot_module.service, "status", side_effect=RuntimeError("network blip")):
        bot._handle_command(_message(OWNER_CHAT_ID, "/status"), "/status")  # must not raise
    assert "network blip" in api.sent[-1]["text"]


def test_unknown_command_gets_a_hint_not_silence():
    bot, api = _make_bot()
    bot._handle_command(_message(OWNER_CHAT_ID, "/frobnicate"), "/frobnicate")
    assert "/help" in api.sent[-1]["text"]


def test_run_forever_survives_a_transient_getupdates_failure():
    bot, api = _make_bot()
    api.queue_updates(RuntimeError("temporary network blip"))
    api.queue_updates([])
    with mock.patch("time.sleep"):  # do not actually wait out the backoff in tests
        bot.run_forever(max_iterations=2)
    assert api.get_updates_calls == 2  # the failure did not abort the loop


def test_long_reply_is_chunked_and_keyboard_rides_the_last_chunk():
    bot, api = _make_bot()
    huge_text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))  # well over 4096 chars
    keyboard = {"inline_keyboard": [[{"text": "Confirm", "callback_data": "confirm:z"}]]}
    bot._reply(OWNER_CHAT_ID, keyboard, huge_text)
    assert len(api.sent) > 1
    assert all(m["reply_markup"] is None for m in api.sent[:-1])
    assert api.sent[-1]["reply_markup"] == keyboard
