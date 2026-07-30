"""Characterization tests for the confirm gate in `service.py`.

`service.py` is the facade every front end goes through - CLI, Telegram, and
anything built later - so the confirm contract is enforced here or nowhere:

  confirm=False        -> priced plan back, needs_confirmation, NOTHING sent
  confirm=True         -> sent, but only after `_guard_confirmed` agrees the
                          plan has not grown past what was actually approved
  blockers present     -> refused outright, no pointless confirmation prompt

`_guard_confirmed` is the race-condition guard: between showing a preview and
the owner tapping Confirm, a fill can land and "sell all" can mean more shares
than were on screen. Growth is refused; shrink is allowed, because selling
less than was approved needs no new approval.

All offline - no client, no orders.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot import service, trading
from polymarket_bot.config import Settings

# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------


def _settings(**kw) -> Settings:
    return Settings(private_key="0x0", wallet="0xw", data_dir=Path("."), **kw)


@pytest.fixture(autouse=True)
def _isolated_settings():
    """Never let these tests read the developer's real .env."""
    with mock.patch.object(service, "load_settings", return_value=_settings()):
        yield


def _buy_plan(usdc=5.0, *, blockers=None) -> trading.OrderPlan:
    return trading.OrderPlan(
        kind="market", side="BUY", market_title="Probe?", condition_id="0xc",
        token_id="TOK", outcome_label="Yes", usdc_amount=usdc, shares=None,
        limit_price=None, est_price=0.50, est_shares=usdc / 0.50,
        est_proceeds=None, min_order_size=5.0, tick_size=0.01,
        protect_price=0.55, warnings=[], blockers=list(blockers or []),
    )


def _sell_plan(shares=100.0, *, blockers=None) -> trading.OrderPlan:
    return trading.OrderPlan(
        kind="market", side="SELL", market_title="Probe?", condition_id="0xc",
        token_id="TOK", outcome_label="Yes", usdc_amount=None, shares=shares,
        limit_price=None, est_price=0.50, est_shares=shares,
        est_proceeds=shares * 0.50, min_order_size=5.0, tick_size=0.01,
        protect_price=0.45, warnings=[], blockers=list(blockers or []),
    )


class _ExecuteSpy:
    def __init__(self):
        self.calls: list[trading.OrderPlan] = []

    def __call__(self, client, settings, plan, *, notifier=None):
        self.calls.append(plan)
        return trading.TradeResult(
            ok=True, status="matched", order_id="0xorder",
            filled_shares=plan.shares or 10.0, filled_usdc=plan.usdc_amount or 5.0,
            avg_price=0.50, tx_hashes=[], error=None, plan=plan,
        )


def _wire(plan, spy=None):
    """Patch the seams a buy/sell walks: market lookup, plan build, execute."""
    builder = "build_buy_plan" if plan.side == "BUY" else "build_sell_plan"
    return (
        mock.patch.object(service, "_resolve_market", return_value=SimpleNamespace()),
        mock.patch.object(trading, builder, return_value=plan),
        mock.patch.object(trading, "execute_plan", spy or _ExecuteSpy()),
    )


def _run(fn, plan, spy, **kwargs):
    market_patch, build_patch, exec_patch = _wire(plan, spy)
    with market_patch, build_patch, exec_patch:
        return fn("probe-market", "yes", client=SimpleNamespace(), **kwargs)


# ---------------------------------------------------------------------------
# confirm=False sends nothing
# ---------------------------------------------------------------------------


def test_buy_without_confirm_sends_nothing_and_asks_for_confirmation():
    spy = _ExecuteSpy()
    response = _run(service.buy, _buy_plan(5.0), spy, usd=5.0)

    assert response["ok"] is False, "an unconfirmed buy must not report success"
    assert response["needs_confirmation"] is True
    assert response["executed"] is False if "executed" in response else True
    assert spy.calls == [], "an unconfirmed buy reached the exchange"


def test_sell_without_confirm_sends_nothing_and_asks_for_confirmation():
    spy = _ExecuteSpy()
    response = _run(service.sell, _sell_plan(100.0), spy, shares=100.0)

    assert response["needs_confirmation"] is True
    assert spy.calls == [], "an unconfirmed sell reached the exchange"


def test_unconfirmed_response_carries_replayable_confirm_args():
    # The Telegram Confirm button replays these verbatim; a missing
    # expected_* would silently disable the drift guard on the confirm leg.
    spy = _ExecuteSpy()
    response = _run(service.buy, _buy_plan(5.0), spy, usd=5.0)

    args = response["confirm_args"]
    assert args["confirm"] is True
    assert args["usd"] == 5.0
    assert args["expected_usdc"] == 5.0
    assert "limit_price" in args, "dropping limit_price turns a limit order into a market order"


def test_sell_confirm_args_never_carry_fraction():
    # "sell all" re-resolves against the live position - that is exactly how a
    # confirmation for 10 shares becomes a sale of 15.
    spy = _ExecuteSpy()
    response = _run(service.sell, _sell_plan(100.0), spy, fraction=1.0)

    assert "fraction" not in response["confirm_args"]
    assert response["confirm_args"]["shares"] == 100.0


# ---------------------------------------------------------------------------
# blockers short-circuit the prompt
# ---------------------------------------------------------------------------


def test_a_blocked_buy_is_refused_rather_than_offered_for_confirmation():
    spy = _ExecuteSpy()
    response = _run(
        service.buy, _buy_plan(25.0, blockers=["over the $5.00 per-order cap"]), spy, usd=25.0
    )

    assert response["ok"] is False
    assert response.get("needs_confirmation") is not True, (
        "a plan that can never execute was offered for confirmation"
    )
    assert spy.calls == []


# ---------------------------------------------------------------------------
# the drift guard
# ---------------------------------------------------------------------------


def test_confirmed_buy_that_grew_beyond_the_approved_spend_is_refused():
    # Approved $5; the rebuilt plan now wants $9.
    spy = _ExecuteSpy()
    response = _run(service.buy, _buy_plan(9.0), spy, usd=9.0, confirm=True, expected_usdc=5.0)

    assert response["ok"] is False
    assert "grew" in response["text"].lower()
    assert spy.calls == [], "an order larger than what was confirmed was sent"


def test_confirmed_sell_that_grew_beyond_the_approved_size_is_refused():
    spy = _ExecuteSpy()
    response = _run(
        service.sell, _sell_plan(150.0), spy, shares=150.0, confirm=True, expected_shares=100.0
    )

    assert response["ok"] is False
    assert spy.calls == [], "a sell larger than what was confirmed was sent"


def test_a_plan_that_shrank_is_allowed_through():
    """Selling less than was approved needs no new approval - refusing it would
    strand an exit whenever the owner sold part of the position elsewhere."""
    spy = _ExecuteSpy()
    response = _run(
        service.sell, _sell_plan(60.0), spy, shares=60.0, confirm=True, expected_shares=100.0
    )

    assert response["ok"] is True
    assert len(spy.calls) == 1
    assert spy.calls[0].shares == 60.0


def test_confirmed_and_unchanged_goes_through():
    spy = _ExecuteSpy()
    response = _run(service.buy, _buy_plan(5.0), spy, usd=5.0, confirm=True, expected_usdc=5.0)

    assert response["ok"] is True
    assert len(spy.calls) == 1


def test_a_nonsense_expected_value_is_refused_not_ignored():
    # A malformed guard value must fail closed. Treating it as "no expectation"
    # would let a corrupted confirm payload disable the check entirely.
    spy = _ExecuteSpy()
    response = _run(service.buy, _buy_plan(5.0), spy, usd=5.0, confirm=True, expected_usdc=0.0)

    assert response["ok"] is False
    assert spy.calls == []


def test_guard_is_skipped_only_when_no_expectation_was_supplied():
    # Backwards compatibility for a caller that never passed expected_*; the
    # plan still executes, but this is the one path with no drift protection.
    spy = _ExecuteSpy()
    response = _run(service.buy, _buy_plan(5.0), spy, usd=5.0, confirm=True)

    assert response["ok"] is True
    assert len(spy.calls) == 1


# ---------------------------------------------------------------------------
# the _safe backstop
# ---------------------------------------------------------------------------


def test_an_unexpected_exception_becomes_a_failed_response_not_a_traceback():
    # A Telegram handler has nowhere to put a traceback.
    with mock.patch.object(service, "_resolve_market", side_effect=RuntimeError("socket died")):
        response = service.buy("probe", "yes", usd=5.0, client=SimpleNamespace())

    assert response["ok"] is False
    assert "socket died" in response["error"]


def test_bad_user_input_keeps_its_own_message():
    with mock.patch.object(
        service, "_resolve_market", side_effect=ValueError("No market matches 'nonsense'.")
    ):
        response = service.buy("nonsense", "yes", usd=5.0, client=SimpleNamespace())

    assert response["ok"] is False
    # ValueError messages are already written for a human - not buried behind
    # a type name.
    assert response["error"] == "No market matches 'nonsense'."
