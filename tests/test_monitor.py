"""Characterization tests for the money gates in `monitor.py`.

The monitor is the only thing that can sell without a human tapping Confirm,
so what matters is the set of gates that stand between a triggered rule and a
real order:

  dry-run  -> nothing is built or sent, whatever else is true
  halted   -> the trigger is reported, the sell is not made
  live     -> exactly one sell, once, for the shares the decision named

Plus the fail-closed rule on the loss ledger: a budget that cannot read its
own records must behave as though it is spent, not as though it is empty.

All offline. The fake client's sell path records instead of sending, so a test
that wrongly reaches it fails on the assertion rather than on the network.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from polymarket_bot import monitor as mon
from polymarket_bot import trading
from polymarket_bot.config import Settings
from polymarket_bot.notify import CollectingNotifier
from polymarket_bot.portfolio import PositionView
from polymarket_bot.rules import ExitRule, RuleStore

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _settings(tmp: Path, **kw) -> Settings:
    return Settings(private_key="0x0", wallet="0xw", data_dir=tmp, **kw)


def _rule(rid="r1", kind="stop_loss", *, target_price=0.30, exit_fraction=1.0) -> ExitRule:
    return ExitRule(
        id=rid, condition_id="0xc", token_id="TOK", market_title="Probe?",
        outcome="Yes", kind=kind, target_price=target_price, target_pct=None,
        trail_pct=None, exit_fraction=exit_fraction,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _position(shares=100.0, cur_price=0.20) -> PositionView:
    """Priced below a 0.30 stop, so a stop_loss rule fires on it."""
    return PositionView(
        condition_id="0xc", token_id="TOK", opposite_token_id="OTHER",
        market_title="Probe?", slug="probe", outcome="Yes",
        shares=shares, avg_price=0.60, cur_price=cur_price,
        cost_basis=shares * 0.60, current_value=shares * cur_price,
        unrealized_pnl=-40.0, unrealized_pnl_pct=-66.0, realized_pnl=0.0,
        redeemable=False, is_resolved=False, end_date=None,
    )


class _SellRecorder:
    """Stands in for the whole build-plan/execute path below `_execute`."""

    def __init__(self, *, ok=True):
        self.calls: list[float] = []
        self._ok = ok

    def execute_plan(self, client, settings, plan, *, notifier=None):
        self.calls.append(plan.shares or 0.0)
        return trading.TradeResult(
            ok=self._ok, status="matched", order_id="0xorder",
            filled_shares=plan.shares or 0.0, filled_usdc=(plan.shares or 0.0) * 0.20,
            avg_price=0.20, tx_hashes=[], error=None if self._ok else "rejected",
            plan=plan,
        )


def _armed_monitor(settings, store, *, notifier=None, price=0.20, position=None):
    """A Monitor with live state stubbed at the seams `run_once` actually reads.

    Those seams are `portfolio.get_positions` (a module function called on the
    client, NOT a Monitor method) and the `_price_for` method. Stubbing there
    rather than at the SDK keeps these tests about the gates, not about book
    parsing - tests/test_trading.py already covers that.
    """
    monitor = mon.Monitor(client=SimpleNamespace(), settings=settings, store=store, notifier=notifier)
    monitor._price_for = lambda token_id, position, cache: mon._PriceRead(  # type: ignore[attr-defined]
        price=price, source="book midpoint", error=None, trusted=True
    )
    return monitor


def _positions(position):
    """Patch the module function `run_once` calls to read live positions."""
    return mock.patch.object(mon.portfolio, "get_positions", return_value=[position])


def _write_ledger(settings: Settings, *, loss: float, hours_ago: float = 1.0) -> None:
    settings.ensure_data_dir()
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    settings.state_path.write_text(
        json.dumps({"realized_exits": [{"at": when.isoformat(), "loss": loss, "pnl": -loss}]}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# the loss budget
# ---------------------------------------------------------------------------


def test_losses_inside_the_window_count_toward_the_budget():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), daily_loss_limit_usdc=10.0)
        _write_ledger(settings, loss=4.0, hours_ago=1.0)
        monitor = mon.Monitor(client=SimpleNamespace(), settings=settings)
        assert monitor.loss_window_usdc() == 4.0


def test_losses_older_than_the_window_do_not_count():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), daily_loss_limit_usdc=10.0)
        _write_ledger(settings, loss=99.0, hours_ago=48.0)
        monitor = mon.Monitor(client=SimpleNamespace(), settings=settings)
        assert monitor.loss_window_usdc() == 0.0


def test_a_spent_budget_halts_execution():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), daily_loss_limit_usdc=10.0)
        _write_ledger(settings, loss=12.0)
        monitor = mon.Monitor(client=SimpleNamespace(), settings=settings)
        assert "Daily loss limit reached" in (monitor._halt_reason() or "")


def test_an_unreadable_ledger_halts_execution_fail_closed():
    """An unreadable ledger is indistinguishable from one full of losses.
    Reading it as a fresh $0 budget is the failure mode this prevents."""
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), daily_loss_limit_usdc=10.0)
        monitor = mon.Monitor(client=SimpleNamespace(), settings=settings)
        with mock.patch.object(mon, "_load_state", side_effect=OSError("disk error")):
            reason = monitor._halt_reason()
        assert reason is not None
        assert "treated as spent" in reason


def test_a_zero_limit_disables_the_check_rather_than_halting_forever():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), daily_loss_limit_usdc=0.0)
        _write_ledger(settings, loss=500.0)
        monitor = mon.Monitor(client=SimpleNamespace(), settings=settings)
        assert monitor._halt_reason() is None


# ---------------------------------------------------------------------------
# the gates between a trigger and a real order
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_trigger_and_sends_nothing():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=True)
        store = RuleStore(settings)
        store.add(_rule())
        recorder = _SellRecorder()
        monitor = _armed_monitor(settings, store)

        with _positions(_position()), mock.patch.object(trading, "execute_plan", recorder.execute_plan):
            report = monitor.run_once()

        assert len(report.triggered) == 1, "the stop-loss did not fire at all"
        assert report.triggered[0]["action"] == "dry_run"
        assert recorder.calls == [], "a dry run sent an order"


def test_a_halted_monitor_reports_the_trigger_but_does_not_sell():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=False, daily_loss_limit_usdc=10.0)
        _write_ledger(settings, loss=12.0)  # budget already spent
        store = RuleStore(settings)
        store.add(_rule())
        recorder = _SellRecorder()
        monitor = _armed_monitor(settings, store)

        with _positions(_position()), mock.patch.object(trading, "execute_plan", recorder.execute_plan):
            report = monitor.run_once()

        assert report.halted_reason is not None
        assert len(report.triggered) == 1
        assert report.triggered[0]["action"] == "halted"
        assert recorder.calls == [], "a halted monitor sold anyway"


def test_dry_run_beats_a_halt_and_still_reports_both():
    # Dry run wins over every other gate; the report must still say it was
    # also halted, or a reader thinks the budget had room.
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=True, daily_loss_limit_usdc=10.0)
        _write_ledger(settings, loss=12.0)
        store = RuleStore(settings)
        store.add(_rule())
        monitor = _armed_monitor(settings, store)

        with _positions(_position()):
            report = monitor.run_once()

        assert report.triggered[0]["action"] == "dry_run"
        assert "halted" in report.triggered[0]["note"].lower()


def test_a_rule_that_does_not_fire_sends_nothing():
    # Control: the gates above are only meaningful if an untriggered rule is
    # actually quiet. Price 0.80 is far above the 0.30 stop.
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=False)
        store = RuleStore(settings)
        store.add(_rule())
        recorder = _SellRecorder()
        monitor = _armed_monitor(settings, store, price=0.80)

        with _positions(_position(cur_price=0.80)), \
             mock.patch.object(trading, "execute_plan", recorder.execute_plan):
            report = monitor.run_once()

        assert report.triggered == []
        assert recorder.calls == []


def test_a_live_trigger_sells_exactly_the_shares_the_decision_named():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=False, daily_loss_limit_usdc=0.0)
        store = RuleStore(settings)
        store.add(_rule(exit_fraction=0.5))  # exit half of 100 shares
        recorder = _SellRecorder()
        monitor = _armed_monitor(settings, store)

        with _positions(_position()), \
             mock.patch.object(trading, "execute_plan", recorder.execute_plan), \
             mock.patch.object(mon, "get_market_by_condition_id", return_value=SimpleNamespace()), \
             mock.patch.object(mon, "_outcome_for_token", return_value="yes"), \
             mock.patch.object(
                 trading, "build_sell_plan",
                 side_effect=lambda *a, **kw: _executable_plan(kw["shares"]),
             ):
            report = monitor.run_once()

        assert len(report.triggered) == 1
        assert recorder.calls == [50.0], f"expected one 50-share sell, got {recorder.calls}"
        assert report.executed[0]["ok"] is True


def test_a_blocked_exit_leaves_the_rule_armed_for_the_next_sweep():
    """A blocker may clear. Retiring a stop-loss because one sweep could not
    build the order removes the protection it exists for."""
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=False, daily_loss_limit_usdc=0.0)
        store = RuleStore(settings)
        store.add(_rule())
        recorder = _SellRecorder()
        monitor = _armed_monitor(settings, store)

        with _positions(_position()), \
             mock.patch.object(trading, "execute_plan", recorder.execute_plan), \
             mock.patch.object(mon, "get_market_by_condition_id", return_value=SimpleNamespace()), \
             mock.patch.object(mon, "_outcome_for_token", return_value="yes"), \
             mock.patch.object(
                 trading, "build_sell_plan",
                 side_effect=lambda *a, **kw: _blocked_plan("market is halted"),
             ):
            report = monitor.run_once()

        assert recorder.calls == [], "a blocked plan was sent"
        assert report.executed[0]["blockers"] == ["market is halted"]
        assert [r.id for r in store.list(active_only=True)] == ["r1"], "rule was retired on a blocker"


def test_a_halt_is_announced_once_not_every_sweep():
    # A halted monitor that re-alerts every interval trains the owner to mute
    # the chat -- which is where the real stop-loss alerts go too.
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=True, daily_loss_limit_usdc=10.0)
        _write_ledger(settings, loss=12.0)
        store = RuleStore(settings)
        store.add(_rule())
        notifier = CollectingNotifier()
        monitor = _armed_monitor(settings, store, notifier=notifier)

        with _positions(_position()):
            for _ in range(4):
                monitor.run_once()

        halts = [m for lvl, m in notifier.messages if "Daily loss limit reached" in m]
        assert len(halts) == 1, f"halt announced {len(halts)} times across 4 sweeps"


# ---------------------------------------------------------------------------
# plan builders for the tests above
# ---------------------------------------------------------------------------


def _executable_plan(shares: float) -> trading.OrderPlan:
    return trading.OrderPlan(
        kind="market", side="SELL", market_title="Probe?", condition_id="0xc",
        token_id="TOK", outcome_label="Yes", usdc_amount=None, shares=shares,
        limit_price=None, est_price=0.20, est_shares=shares,
        est_proceeds=shares * 0.20, min_order_size=5.0, tick_size=0.01,
        protect_price=0.18, warnings=[], blockers=[],
    )


def _blocked_plan(reason: str) -> trading.OrderPlan:
    plan = _executable_plan(100.0)
    plan.blockers = [reason]
    return plan
