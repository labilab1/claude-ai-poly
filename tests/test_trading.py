"""Characterization tests for the money paths in `trading.py`.

`trading.py` is the only module that can send an order, and it was the largest
file in the project with no test of its own - everything exercising it did so
indirectly. These pin the behaviours that decide whether real money moves:

  * the per-order cap applies to BUYS and not to SELLS (capping sell proceeds
    is what silently disabled every stop-loss once, see test_safety_regressions);
  * `execute_plan` refuses a blocked plan, refuses again if live state drifted,
    and refuses a market order with no slippage guard;
  * a rejected or throwing SDK call becomes `TradeResult(ok=False)`, never an
    exception - a monitor sweeping many positions must not abort on one.

All offline. `_FakeClient.place_market_order` asserts if a test ever reaches a
real send path it did not mean to.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from polymarket_bot import trading
from polymarket_bot.config import Settings
from polymarket_bot.notify import CollectingNotifier

# ---------------------------------------------------------------------------
# fakes (same shapes as tests/test_safety_regressions.py)
# ---------------------------------------------------------------------------


def _book(bids, asks, tick=0.01, min_size=5):
    lvl = lambda p, s: SimpleNamespace(price=p, size=s)  # noqa: E731
    return SimpleNamespace(
        bids=[lvl(p, s) for p, s in bids],  # ASC, best bid last
        asks=[lvl(p, s) for p, s in asks],  # DESC, best ask last
        tick_size=tick,
        min_order_size=min_size,
        neg_risk=False,
    )


def _market(token="TOK", accepting=True):
    return SimpleNamespace(
        id="1", slug="probe-market", condition_id="0xcond",
        question="Probe market?", description="",
        state=SimpleNamespace(active=True, closed=False, accepting_orders=accepting, end_date=None),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(label="Yes", token_id=token, price=0.5),
            no=SimpleNamespace(label="No", token_id="OTHER", price=0.5),
        ),
        trading=SimpleNamespace(minimum_order_size=5, minimum_tick_size=0.01),
        metrics=SimpleNamespace(volume_24hr=0, liquidity_num=0),
        prices=SimpleNamespace(best_bid=None, best_ask=None, spread=None, one_day_price_change=0),
        rewards=SimpleNamespace(clob_rewards=(), rewards_min_size=None),
        tags=(),
    )


class _FakeClient:
    """Reads only, unless a test explicitly supplies an order response."""

    def __init__(self, book, est, held, *, cash=100.0, order_response=None, order_raises=None):
        self._book, self._est, self._held = book, est, held
        self._cash = cash
        self._order_response = order_response
        self._order_raises = order_raises
        self.sent: list[dict] = []

    def get_order_book(self, *, token_id):
        return self._book

    def estimate_market_price(self, **kw):
        return self._est

    def get_balance_allowance(self, **kw):
        # int, 6dp - the SDK's own shape. portfolio divides by 1e6.
        return SimpleNamespace(balance=int(self._cash * 1_000_000))

    def list_positions(self, **kw):
        pos = SimpleNamespace(
            condition_id="0xcond", token_id="TOK", opposite_token_id="OTHER",
            size=self._held, avg_price=0.60, cur_price=0.50,
            initial_value=self._held * 0.60, current_value=self._held * 0.50,
            cash_pnl=0.0, percent_pnl=0.0, realized_pnl=0.0,
            redeemable=False, mergeable=False, title="Probe market?",
            slug="probe-market", outcome="Yes", outcome_index=0, end_date=None,
        )
        return SimpleNamespace(iter_items=lambda: iter([pos]))

    def list_open_orders(self, **kw):
        return SimpleNamespace(iter_items=lambda: iter([]))

    def place_market_order(self, **kw):
        self.sent.append(kw)
        if self._order_raises is not None:
            raise self._order_raises
        if self._order_response is None:
            raise AssertionError("test reached a send path without supplying a response")
        return self._order_response

    def place_limit_order(self, **kw):
        self.sent.append(kw)
        if self._order_raises is not None:
            raise self._order_raises
        if self._order_response is None:
            raise AssertionError("test reached a send path without supplying a response")
        return self._order_response


def _settings(**kw) -> Settings:
    return Settings(private_key="0x0", wallet="0xw", data_dir=Path("."), **kw)


def _liquid_client(*, held=100, **kw):
    """A book deep enough that nothing is blocked for liquidity reasons.

    `held=0` is what a BUY test wants: the fake position is 100 shares at 0.60,
    a $60 cost basis that trips the $10 per-market position cap on its own.
    """
    return _FakeClient(
        _book(bids=[(0.86, 5000), (0.88, 4000), (0.89, 3000)], asks=[(0.90, 3000), (0.91, 5000)]),
        est=0.885,
        held=held,
        **kw,
    )


def _accepted(*, making=100.0, taking=88.5, **overrides):
    """An AcceptedOrder. `_read_fill` reads making/taking, not named fill fields:
    a SELL makes shares and takes USDC; a BUY is the other way round."""
    base = dict(
        ok=True, status="matched", order_id="0xorder",
        making_amount=making, taking_amount=taking,
        transactions_hashes=("0xtx",),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@contextlib.contextmanager
def _live(market):
    """`execute_plan` always re-reads the market through `_rebuild`.

    That lookup is a module-level function, not a client method, so the fake
    client cannot serve it - patch it to control what the re-verify pass sees.
    """
    with mock.patch.object(trading, "get_market_by_condition_id", return_value=market):
        yield


# ---------------------------------------------------------------------------
# the per-order cap: BUYS only
# ---------------------------------------------------------------------------


def test_buy_over_the_per_order_cap_is_blocked():
    plan = trading.build_buy_plan(
        _liquid_client(), _settings(max_order_usdc=5.0),
        market=_market(), outcome="yes", usdc_amount=25.0,
    )
    assert not plan.is_executable()
    assert any("per-order cap" in b for b in plan.blockers)


def test_buy_at_exactly_the_cap_is_allowed():
    # The cap is a ceiling, not an exclusive bound - an off-by-one here would
    # quietly shrink every position size.
    plan = trading.build_buy_plan(
        _liquid_client(), _settings(max_order_usdc=5.0),
        market=_market(), outcome="yes", usdc_amount=5.0,
    )
    assert not any("per-order cap" in b for b in plan.blockers)


def test_sell_worth_far_more_than_the_cap_is_still_executable():
    """The documented design decision: capping sell proceeds made any position
    worth more than the cap permanently un-exitable, disabling every stop-loss.
    100 shares near 0.885 is ~$88 of proceeds against a $5 cap."""
    plan = trading.build_sell_plan(
        _liquid_client(), _settings(max_order_usdc=5.0),
        market=_market(), outcome="yes", shares=100,
    )
    assert plan.is_executable(), f"exit refused: {plan.blockers}"
    assert plan.est_proceeds is not None and plan.est_proceeds > 5.0
    assert not any("cap" in b.lower() for b in plan.blockers)


def test_buy_amount_of_nan_is_rejected():
    # NaN passes every comparison ("nan > 5.0" is False), so it would slip the
    # cap unchecked if it were not named explicitly.
    plan = trading.build_buy_plan(
        _liquid_client(), _settings(max_order_usdc=5.0),
        market=_market(), outcome="yes", usdc_amount=float("nan"),
    )
    assert not plan.is_executable()
    assert any("finite" in b for b in plan.blockers)


def test_buy_on_a_market_not_accepting_orders_is_blocked():
    plan = trading.build_buy_plan(
        _liquid_client(), _settings(),
        market=_market(accepting=False), outcome="yes", usdc_amount=1.0,
    )
    assert not plan.is_executable()
    assert any("not accepting orders" in b for b in plan.blockers)


# ---------------------------------------------------------------------------
# execute_plan: refusal paths
# ---------------------------------------------------------------------------


def test_execute_refuses_a_plan_that_already_has_blockers():
    client = _liquid_client()
    plan = trading.build_buy_plan(
        client, _settings(max_order_usdc=5.0),
        market=_market(), outcome="yes", usdc_amount=25.0,  # over cap
    )
    result = trading.execute_plan(client, _settings(max_order_usdc=5.0), plan)

    assert result.ok is False
    assert "plan blockers" in (result.error or "")
    assert client.sent == [], "a blocked plan reached the exchange"


def test_execute_refuses_when_live_state_has_drifted_into_a_blocker():
    """The re-verify pass exists because everything the plan asserted could have
    changed. Here the market stops accepting orders between plan and execute."""
    client = _liquid_client(order_response=_accepted())
    settings = _settings()
    plan = trading.build_sell_plan(client, settings, market=_market(), outcome="yes", shares=100)
    assert plan.is_executable()

    with _live(_market(accepting=False)):
        result = trading.execute_plan(client, settings, plan)

    assert result.ok is False
    assert "live re-check" in (result.error or "")
    assert client.sent == [], "an order was sent despite the live re-check failing"


def test_execute_refuses_when_the_market_cannot_be_re_read_at_all():
    # Unverifiable state is a refusal, not a shrug: this is the fail-closed rule.
    client = _liquid_client(order_response=_accepted())
    settings = _settings()
    plan = trading.build_sell_plan(client, settings, market=_market(), outcome="yes", shares=100)

    with mock.patch.object(
        trading, "get_market_by_condition_id", side_effect=RuntimeError("gamma down")
    ):
        result = trading.execute_plan(client, settings, plan)

    assert result.ok is False
    assert "re-verify" in (result.error or "")
    assert client.sent == []


def test_execute_reports_a_thrown_sdk_error_instead_of_raising():
    # A monitor sweeping many positions must not abort on one bad market.
    client = _liquid_client(order_raises=RuntimeError("connection reset"))
    settings = _settings()
    plan = trading.build_sell_plan(client, settings, market=_market(), outcome="yes", shares=100)

    with _live(_market()):
        result = trading.execute_plan(client, settings, plan)

    assert result.ok is False
    assert "connection reset" in (result.error or "")


def test_execute_reports_an_exchange_rejection_as_a_failed_result():
    rejected = SimpleNamespace(ok=False, code="INVALID_ORDER", message="min size not met")
    client = _liquid_client(order_response=rejected)
    settings = _settings()
    plan = trading.build_sell_plan(client, settings, market=_market(), outcome="yes", shares=100)

    with _live(_market()):
        result = trading.execute_plan(client, settings, plan)

    assert result.ok is False
    assert "min size not met" in (result.error or "")


# ---------------------------------------------------------------------------
# execute_plan: the slippage guard actually reaches the exchange
# ---------------------------------------------------------------------------


def test_market_sell_sends_a_min_price_floor():
    client = _liquid_client(order_response=_accepted())
    settings = _settings()
    plan = trading.build_sell_plan(client, settings, market=_market(), outcome="yes", shares=100)

    with _live(_market()):
        result = trading.execute_plan(client, settings, plan)

    assert result.ok is True
    assert len(client.sent) == 1
    sent = client.sent[0]
    assert sent["side"] == "SELL"
    assert sent["order_type"] == "FAK"
    assert sent["min_price"] is not None and sent["min_price"] > 0
    # A floor above the realistic fill can never fill - the dust-book bug.
    assert plan.est_price is not None and sent["min_price"] <= plan.est_price


def test_market_buy_sends_a_max_price_ceiling_and_a_usdc_amount():
    # held=0: the fake position's $60 cost basis would otherwise trip the
    # per-market cap before the order shape could be checked at all.
    client = _liquid_client(held=0, order_response=_accepted(making=5.0, taking=5.6))
    settings = _settings(max_order_usdc=5.0)
    plan = trading.build_buy_plan(
        client, settings, market=_market(), outcome="yes", usdc_amount=5.0
    )
    assert plan.is_executable(), plan.blockers

    with _live(_market()):
        result = trading.execute_plan(client, settings, plan)

    assert result.ok is True
    sent = client.sent[0]
    assert sent["side"] == "BUY"
    # BUY market orders are denominated in USDC, not shares.
    assert sent["amount"] == 5.0 and "shares" not in sent
    assert sent["max_price"] is not None and sent["max_price"] > 0


def test_execute_emits_a_trade_level_event_for_a_fill():
    # The monitor's Telegram push only forwards trade/alert/error; a fill that
    # emitted at "info" would never reach a phone.
    client = _liquid_client(order_response=_accepted())
    settings = _settings()
    plan = trading.build_sell_plan(client, settings, market=_market(), outcome="yes", shares=100)
    notifier = CollectingNotifier()

    with _live(_market()):
        trading.execute_plan(client, settings, plan, notifier=notifier)

    levels = [level for level, _msg in notifier.messages]
    assert "trade" in levels


def test_execute_emits_an_alert_when_it_refuses():
    client = _liquid_client()
    settings = _settings(max_order_usdc=5.0)
    plan = trading.build_buy_plan(
        client, settings, market=_market(), outcome="yes", usdc_amount=25.0
    )
    notifier = CollectingNotifier()

    trading.execute_plan(client, settings, plan, notifier=notifier)

    levels = [level for level, _msg in notifier.messages]
    assert "alert" in levels
