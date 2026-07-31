"""Tests for selling a holding from the Positions screen.

Before this the menu could BUY but not sell: the portfolio screen was a text
dump with no per-position actions, so the only way out of a position was a
typed /sell command. That asymmetry is what these cover - plus the rule that
a sell button is a shortcut to the PREVIEW, never to the order.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot.config import Settings
from polymarket_bot.telegram import bot as bot_module
from polymarket_bot.telegram import menu as menu_mod
from polymarket_bot.telegram.bot import TelegramBot

OWNER = 12345


class FakeAPI:
    def __init__(self):
        self.sent: list[dict] = []
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
        return {}

    def answer_callback_query(self, *a, **k):
        return {}

    def all_text(self):
        return "\n".join(s["text"] for s in self.screens)

    def last_inline(self):
        for entry in reversed(self.screens):
            markup = entry.get("reply_markup") or {}
            if "inline_keyboard" in markup:
                return markup["inline_keyboard"]
        return []

    def payloads(self):
        return [b.get("callback_data", "") for row in self.last_inline() for b in row]

    def labels(self):
        return [b.get("text", "") for row in self.last_inline() for b in row]


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


def _position(slug="market-a", outcome="Yes", *, shares=120.0, resolved=False):
    return {
        "condition_id": f"0x{slug}", "slug": slug, "market_title": f"Will {slug} happen?",
        "outcome": outcome, "shares": shares, "avg_price": 0.40, "cur_price": 0.55,
        "cost_basis": shares * 0.40, "current_value": shares * 0.55,
        "unrealized_pnl": shares * 0.15, "unrealized_pnl_pct": 37.5,
        "is_resolved": resolved, "redeemable": resolved,
    }


def _positions(*rows):
    return {
        "ok": True, "text": "raw", "positions": list(rows), "count": len(rows),
        "total_value": sum(r["current_value"] for r in rows),
        "total_unrealized_pnl": sum(r["unrealized_pnl"] for r in rows),
    }


def _open_positions(bot, api, *rows):
    with mock.patch.object(bot_module.service, "positions", return_value=_positions(*rows)):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_PORTFOLIO}:"))


def _first_position_token(api) -> str:
    for payload in api.payloads():
        if payload.startswith(f"nav:{menu_mod.VIEW_POSITION}:"):
            return payload.split(":")[-1]
    raise AssertionError(f"no position button: {api.payloads()}")


# ---------------------------------------------------------------------------
# the positions screen
# ---------------------------------------------------------------------------


def test_positions_shows_size_entry_and_pnl(bot_and_api):
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    text = api.all_text()
    assert "120.00" in text          # shares
    assert "40.0" in text            # entry price in cents
    assert "55.0" in text            # current price
    assert "+$18.00" in text         # unrealized P&L


def test_every_holding_gets_its_own_button(bot_and_api):
    bot, api = bot_and_api
    _open_positions(bot, api, _position("a"), _position("b"), _position("c"))
    buttons = [p for p in api.payloads() if p.startswith(f"nav:{menu_mod.VIEW_POSITION}:")]
    assert len(buttons) == 3


def test_an_empty_portfolio_says_so_and_offers_refresh(bot_and_api):
    bot, api = bot_and_api
    _open_positions(bot, api)
    assert "No open positions" in api.all_text()
    assert any("Refresh" in label for label in api.labels())


def test_settled_positions_are_included_so_they_are_not_forgotten(bot_and_api):
    # A settled holding is money waiting to be claimed.
    bot, api = bot_and_api
    with mock.patch.object(
        bot_module.service, "positions", return_value=_positions(_position(resolved=True))
    ) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_PORTFOLIO}:"))
    assert spy.call_args.kwargs["include_resolved"] is True
    assert "SETTLED" in api.all_text()


def test_a_settled_holding_routes_to_redeem_not_sell(bot_and_api):
    """Selling a settled position is an order the exchange will refuse."""
    bot, api = bot_and_api
    _open_positions(bot, api, _position(resolved=True))
    payloads = api.payloads()
    assert any(p == f"nav:{menu_mod.VIEW_MORE}:redeem" for p in payloads)
    assert not any(p.startswith(f"nav:{menu_mod.VIEW_POSITION}:") for p in payloads)


# ---------------------------------------------------------------------------
# one position
# ---------------------------------------------------------------------------


def test_opening_a_position_offers_three_exit_sizes(bot_and_api):
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    token = _first_position_token(api)
    with mock.patch.object(bot_module.service, "positions", return_value=_positions(_position())):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_POSITION}:{token}"))
    sells = [p for p in api.payloads() if p.startswith(f"nav:{menu_mod.VIEW_SELL}:")]
    assert len(sells) == 3


def test_the_position_screen_is_re_read_not_trusted_from_the_list(bot_and_api):
    """The holding may have moved or closed since the list was drawn."""
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    token = _first_position_token(api)
    with mock.patch.object(bot_module.service, "positions", return_value=_positions()) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_POSITION}:{token}"))
    assert spy.called
    assert "no longer open" in api.all_text().lower()


def test_a_position_held_on_both_sides_is_addressed_by_outcome(bot_and_api):
    # A market can be held YES and NO at once; the token has to say which.
    bot, api = bot_and_api
    _open_positions(bot, api, _position("m", "Yes"), _position("m", "No"))
    tokens = [p.split(":")[-1] for p in api.payloads()
              if p.startswith(f"nav:{menu_mod.VIEW_POSITION}:")]
    session = bot._sessions.get(OWNER)
    refs = {session.resolve(tok) for tok in tokens}
    assert refs == {"m|Yes", "m|No"}


# ---------------------------------------------------------------------------
# selling: the safety-critical part
# ---------------------------------------------------------------------------


def _preview():
    return {
        "ok": False, "needs_confirmation": True,
        "confirm_args": {"shares": 120.0, "expected_shares": 120.0,
                         "limit_price": None, "confirm": True},
        "text": "SELL 120 shares of Yes",
    }


def test_a_sell_button_prices_but_does_not_send(bot_and_api):
    """The whole rule: a button reaches the preview, never the order."""
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    token = _first_position_token(api)

    with mock.patch.object(bot_module.service, "sell", return_value=_preview()) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SELL}:a{token}"))

    assert spy.call_args.kwargs.get("confirm", False) is False, "a tap sent an order"
    buttons = [b["callback_data"] for row in api.last_inline() for b in row]
    assert any(b.startswith("confirm:") for b in buttons), "no Confirm button was offered"
    assert len(bot._pending) == 1


@pytest.mark.parametrize("code,fraction", [("a", 1.0), ("h", 0.5), ("q", 0.25)])
def test_each_size_button_sells_its_fraction(bot_and_api, code, fraction):
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    token = _first_position_token(api)
    with mock.patch.object(bot_module.service, "sell", return_value=_preview()) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SELL}:{code}{token}"))
    assert spy.call_args.kwargs["fraction"] == fraction


def test_the_sell_targets_the_outcome_that_was_tapped(bot_and_api):
    bot, api = bot_and_api
    _open_positions(bot, api, _position("m", "No"))
    token = _first_position_token(api)
    with mock.patch.object(bot_module.service, "sell", return_value=_preview()) as spy:
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SELL}:a{token}"))
    assert spy.call_args.args[0] == "m"
    assert spy.call_args.args[1] == "No"


def test_a_stale_sell_token_sends_nothing(bot_and_api):
    bot, api = bot_and_api
    with mock.patch.object(bot_module.service, "sell", side_effect=AssertionError("sold anyway")):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SELL}:adeadbeef"))


def test_an_unknown_size_code_sends_nothing(bot_and_api):
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    token = _first_position_token(api)
    with mock.patch.object(bot_module.service, "sell", side_effect=AssertionError("sold anyway")):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SELL}:z{token}"))


def test_a_refused_sell_offers_no_confirm_button(bot_and_api):
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    token = _first_position_token(api)
    refused = {"ok": False, "text": "SELL REFUSED - market halted", "needs_confirmation": False}
    with mock.patch.object(bot_module.service, "sell", return_value=refused):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SELL}:a{token}"))
    buttons = [b.get("callback_data", "") for row in api.last_inline() for b in row]
    assert not any(b.startswith("confirm:") for b in buttons)
    assert bot._pending == {}


def test_confirming_replays_the_previewed_size_verbatim(bot_and_api):
    """The confirm leg must use the plan's own numbers, never re-derive them
    from the fraction that was tapped."""
    bot, api = bot_and_api
    _open_positions(bot, api, _position())
    token = _first_position_token(api)
    with mock.patch.object(bot_module.service, "sell", return_value=_preview()):
        bot._handle_update(_cb(f"nav:{menu_mod.VIEW_SELL}:a{token}"))
    confirm_token = [b["callback_data"] for row in api.last_inline() for b in row
                     if b.get("callback_data", "").startswith("confirm:")][0].split(":")[1]

    with mock.patch.object(
        bot_module.service, "sell", return_value={"ok": True, "text": "filled"}
    ) as spy:
        bot._handle_update(_cb(f"confirm:{confirm_token}"))

    assert spy.call_args.kwargs["shares"] == 120.0
    assert spy.call_args.kwargs["expected_shares"] == 120.0
    assert "fraction" not in spy.call_args.kwargs, "confirm re-derived the size from a fraction"
