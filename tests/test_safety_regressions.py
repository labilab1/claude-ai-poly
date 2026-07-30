"""Regression tests for money-safety bugs found by adversarial review.

Each test below corresponds to a defect that was PROVEN to exist and has since
been fixed. They are written to fail loudly if the old behaviour ever returns,
because every one of them is silent in production: the bot keeps reporting that
it is protecting a position while it is not.

All offline - fakes only, no network, nothing sent.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot import monitor as mon
from polymarket_bot import rules as rules_mod
from polymarket_bot import trading
from polymarket_bot.config import Settings
from polymarket_bot.portfolio import PositionView
from polymarket_bot.rules import ExitRule, RuleStore

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def _book(bids, asks, tick=0.01, min_size=5):
    lvl = lambda p, s: SimpleNamespace(price=p, size=s)  # noqa: E731
    return SimpleNamespace(
        bids=[lvl(p, s) for p, s in bids],  # ASC, best bid last
        asks=[lvl(p, s) for p, s in asks],  # DESC, best ask last
        tick_size=tick,
        min_order_size=min_size,
        neg_risk=False,
    )


def _market(token="TOK"):
    return SimpleNamespace(
        id="1", slug="probe-market", condition_id="0xcond",
        question="Probe market?", description="",
        state=SimpleNamespace(active=True, closed=False, accepting_orders=True, end_date=None),
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
    """Only the reads a plan needs. Any write path raises."""

    def __init__(self, book, est, held):
        self._book, self._est, self._held = book, est, held

    def get_order_book(self, *, token_id):
        return self._book

    def estimate_market_price(self, **kw):
        return self._est

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
        raise AssertionError("a regression test sent a real order")


def _settings(tmp: Path, **kw) -> Settings:
    return Settings(private_key="0x0", wallet="0xw", data_dir=tmp, **kw)


def _rule(rid: str, kind: str = "stop_loss") -> ExitRule:
    return ExitRule(
        id=rid, condition_id="0xc", token_id="TOK", market_title="Probe?",
        outcome="Yes", kind=kind, target_price=0.30, target_pct=None,
        trail_pct=None, exit_fraction=1.0, created_at="2026-01-01T00:00:00+00:00",
    )


def _position(shares: float = 100.0) -> PositionView:
    return PositionView(
        condition_id="0xc", token_id="TOK", opposite_token_id="OTHER",
        market_title="Probe?", slug="probe", outcome="Yes",
        shares=shares, avg_price=0.60, cur_price=0.50,
        cost_basis=shares * 0.60, current_value=shares * 0.50,
        unrealized_pnl=-10.0, unrealized_pnl_pct=-16.0, realized_pnl=0.0,
        redeemable=False, is_resolved=False, end_date=None,
    )


# --------------------------------------------------------------------------
# trading
# --------------------------------------------------------------------------


def test_slippage_floor_is_fillable_on_a_dust_top_of_book():
    """A 1-share quote at 0.90 over real liquidity at 0.50 must not produce a
    floor above the realistic fill. That floor filled 1 share of 1000 and the
    monitor re-sent it every sweep forever, so the stop-loss never exited."""
    client = _FakeClient(_book(bids=[(0.50, 5000), (0.90, 1)], asks=[(0.95, 100)]),
                         est=0.5004, held=1000)
    plan = trading.build_sell_plan(client, _settings(Path(".")), market=_market(),
                                   outcome="yes", shares=1000)
    if plan.is_executable():
        assert plan.protect_price is not None and plan.est_price is not None
        assert plan.protect_price <= plan.est_price, (
            f"floor {plan.protect_price} is above the estimated fill {plan.est_price}; "
            "this order can never fill"
        )


def test_thin_book_warning_fires_behind_a_penny_wall():
    """Depth must be measured near the touch. Summing the whole book made every
    thin-book warning dead code on any market with a parked $0.01 wall."""
    client = _FakeClient(_book(bids=[(0.01, 50000), (0.89, 50)], asks=[(0.95, 100)]),
                         est=0.88, held=1000)
    facts = trading._facts(client, _market(), "TOK")
    assert facts.snapshot.bid_depth < 1000, "depth counted the whole book again"
    assert facts.snapshot.total_bid_depth > facts.snapshot.bid_depth

    plan = trading.build_sell_plan(client, _settings(Path(".")), market=_market(),
                                   outcome="yes", shares=1000)
    assert any("hin" in w or "hallow" in w for w in plan.warnings), (
        "no thin-book warning for a 1000-share sell into 50 reachable shares"
    )


def test_a_normal_exit_is_not_over_blocked():
    """Control: the protections must not refuse an ordinary sell."""
    client = _FakeClient(_book(bids=[(0.86, 5000), (0.88, 4000), (0.89, 3000)],
                               asks=[(0.91, 3000)]), est=0.885, held=100)
    plan = trading.build_sell_plan(client, _settings(Path(".")), market=_market(),
                                   outcome="yes", shares=100)
    assert plan.is_executable(), f"ordinary exit refused: {plan.blockers}"


# --------------------------------------------------------------------------
# monitor
# --------------------------------------------------------------------------


def test_empty_positions_reads_never_retire_a_rule():
    """An empty /positions response is a failed read, not proof of a sale.
    Counting it retired every rule on the account after a ~3 minute glitch."""
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=True)
        store = RuleStore(settings)
        for i in range(4):
            store.add(_rule(f"rule{i}"))

        monitor = mon.Monitor(client=SimpleNamespace(), settings=settings, store=store)
        monitor._live_positions = lambda: ([], True)  # type: ignore[attr-defined]

        for _ in range(5):
            report = monitor.run_once()
            assert report.deactivated == [], "an empty read retired a rule"

        assert len(store.list(active_only=True)) == 4


def test_a_failed_deactivation_cannot_fire_the_rule_again():
    """If the store write fails the rule must still be dead for this process.
    It used to stay active=True on disk and sell the same position twice."""
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), monitor_dry_run=True)
        store = RuleStore(settings)
        store.add(_rule("boom", kind="take_profit"))

        class _NoPersist:
            def __init__(self, inner): self._inner = inner
            def list(self, **kw): return self._inner.list(**kw)
            def get(self, rid): return self._inner.get(rid)
            def for_token(self, t): return self._inner.for_token(t)
            def add(self, r): return self._inner.add(r)
            def remove(self, rid): return self._inner.remove(rid)
            def update(self, r): raise TimeoutError("another process is writing rules")

        monitor = mon.Monitor(client=SimpleNamespace(), settings=settings, store=_NoPersist(store))
        monitor._deactivate(store.get("boom"), mon.MonitorReport(checked_at="t", dry_run=True),
                            "exit complete")

        assert "boom" in monitor._retired
        fired: list[int] = []
        monitor._evaluate_and_act = lambda *a, **k: fired.append(1)  # type: ignore[attr-defined]
        monitor._process_rule(store.get("boom"), {"TOK": _position()}, {},
                              mon.MonitorReport(checked_at="t2", dry_run=True))
        assert not fired, "a rule retired in memory fired again after a failed persist"


# --------------------------------------------------------------------------
# rule store locking
# --------------------------------------------------------------------------


def test_a_failed_lock_stamp_does_not_orphan_the_lock():
    """The lock file must not survive a failure between create and stamp, or
    every rule edit is blocked until the lock goes stale."""
    with tempfile.TemporaryDirectory() as td:
        store = RuleStore(_settings(Path(td)))
        with mock.patch("os.write", side_effect=OSError(28, "No space left on device")):
            with pytest.raises(OSError):
                store.add(_rule("r1"))

        assert not Path(store.lock_path).exists(), "orphaned lock file"
        store.add(_rule("r2"))  # must not raise
        assert [r.id for r in store.list()] == ["r2"]


def test_lock_acquisition_honours_its_timeout_instead_of_spinning():
    """A contended lock must raise TimeoutError, not spin. The old loop ran
    ~118,000 iterations in 1.5s and never timed out, pegging a core inside the
    monitor sweep."""
    with tempfile.TemporaryDirectory() as td:
        store = RuleStore(_settings(Path(td)))
        attempts = {"n": 0}
        real_open = os.open

        def _always_locked(path, *a, **k):
            if str(path).endswith(".lock"):
                attempts["n"] += 1
                raise FileExistsError(17, "exists")
            return real_open(path, *a, **k)

        with mock.patch.object(rules_mod, "_LOCK_TIMEOUT_SECONDS", 0.20), \
             mock.patch("os.open", side_effect=_always_locked), \
             mock.patch("os.stat", side_effect=FileNotFoundError(2, "gone")):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                store.add(_rule("r3"))
            elapsed = time.monotonic() - started

        assert elapsed < 2.0, f"waited {elapsed:.2f}s for a 0.20s timeout"
        assert attempts["n"] < 500, f"{attempts['n']} retries is a spin, not a wait"
