"""Unit tests for polymarket_bot.rules — pure, offline, no SDK calls.

Run from the repo root:  .\\venv\\Scripts\\python.exe -m pytest tests/test_rules.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Self-contained path bootstrap so the file also runs under a bare `pytest`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_bot import rules as rules_module  # noqa: E402
from polymarket_bot.config import Settings  # noqa: E402
from polymarket_bot.portfolio import PositionView  # noqa: E402
from polymarket_bot.rules import (  # noqa: E402
    ExitRule,
    RuleDecision,
    RuleStore,
    evaluate_rule,
    make_rule,
)

TOKEN = "token-abc"
COND = "0xcondition"


def make_position(**overrides) -> PositionView:
    base = dict(
        condition_id=COND,
        token_id=TOKEN,
        opposite_token_id="token-xyz",
        market_title="Will it rain tomorrow?",
        slug="will-it-rain-tomorrow",
        outcome="Yes",
        shares=10.0,
        avg_price=0.50,
        cur_price=0.50,
        cost_basis=5.0,
        current_value=5.0,
        unrealized_pnl=0.0,
        unrealized_pnl_pct=0.0,
        realized_pnl=0.0,
        redeemable=False,
        is_resolved=False,
        end_date=None,
    )
    base.update(overrides)
    return PositionView(**base)


def make_raw_rule(**overrides) -> ExitRule:
    """Build an ExitRule directly, bypassing make_rule's validation."""
    base = dict(
        id="rule0001",
        condition_id=COND,
        token_id=TOKEN,
        market_title="Will it rain tomorrow?",
        outcome="Yes",
        kind="take_profit",
        target_price=0.70,
    )
    base.update(overrides)
    return ExitRule(**base)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Dummy credentials: nothing in rules.py touches the network.
    return Settings(private_key="0xtest", wallet="0xwallet", data_dir=tmp_path / "data")


# ---------------------------------------------------------------- take_profit


def test_take_profit_fires_at_or_above_target():
    rule = make_raw_rule(kind="take_profit", target_price=0.70)
    decision = evaluate_rule(rule, make_position(), 0.72)
    assert decision.should_exit is True
    assert decision.exit_shares == 10.0
    assert decision.trigger_price == 0.72
    assert decision.rule_id == "rule0001"
    assert "Take-profit hit" in decision.reason

    # exactly at the target counts as hit
    assert evaluate_rule(rule, make_position(), 0.70).should_exit is True


def test_take_profit_does_not_fire_below_target():
    rule = make_raw_rule(kind="take_profit", target_price=0.70)
    decision = evaluate_rule(rule, make_position(), 0.6999)
    assert decision.should_exit is False
    assert decision.exit_shares == 0.0
    assert "not hit" in decision.reason


def test_take_profit_from_target_pct_uses_avg_price():
    # +40% on a 0.50 entry -> 0.70
    rule = make_raw_rule(kind="take_profit", target_price=None, target_pct=40.0)
    position = make_position(avg_price=0.50)
    assert evaluate_rule(rule, position, 0.71).should_exit is True
    assert evaluate_rule(rule, position, 0.69).should_exit is False


def test_take_profit_without_target_cannot_evaluate():
    rule = make_raw_rule(kind="take_profit", target_price=None, target_pct=None)
    decision = evaluate_rule(rule, make_position(), 0.99)
    assert decision.should_exit is False
    assert "cannot evaluate" in decision.reason


# ------------------------------------------------------------------ stop_loss


def test_stop_loss_fires_at_or_below_target():
    rule = make_raw_rule(kind="stop_loss", target_price=0.35)
    decision = evaluate_rule(rule, make_position(), 0.34)
    assert decision.should_exit is True
    assert decision.exit_shares == 10.0
    assert "Stop-loss hit" in decision.reason
    assert evaluate_rule(rule, make_position(), 0.35).should_exit is True


def test_stop_loss_does_not_fire_above_target():
    rule = make_raw_rule(kind="stop_loss", target_price=0.35)
    decision = evaluate_rule(rule, make_position(), 0.36)
    assert decision.should_exit is False
    assert decision.exit_shares == 0.0


def test_stop_loss_from_negative_pct():
    # -30% on a 0.50 entry -> 0.35
    rule = make_raw_rule(kind="stop_loss", target_price=None, target_pct=-30.0)
    position = make_position(avg_price=0.50)
    assert evaluate_rule(rule, position, 0.349).should_exit is True
    assert evaluate_rule(rule, position, 0.351).should_exit is False


# -------------------------------------------------------------- trailing_stop


def test_trailing_stop_does_not_fire_while_price_makes_new_highs():
    rule = make_raw_rule(kind="trailing_stop", target_price=None, trail_pct=10.0, high_water_mark=0.60)
    decision = evaluate_rule(rule, make_position(), 0.80)
    assert decision.should_exit is False
    # the observation itself becomes the effective high (0.80 -> stop 0.72)
    assert "0.7200" in decision.reason


def test_trailing_stop_fires_after_drawdown_from_high_water_mark():
    rule = make_raw_rule(kind="trailing_stop", target_price=None, trail_pct=10.0, high_water_mark=0.80)
    # 10% below 0.80 is 0.72
    assert evaluate_rule(rule, make_position(), 0.7201).should_exit is False
    fired = evaluate_rule(rule, make_position(), 0.72)
    assert fired.should_exit is True
    assert "Trailing stop hit" in fired.reason
    assert evaluate_rule(rule, make_position(), 0.50).should_exit is True


def test_trailing_stop_with_unset_high_water_mark_seeds_from_observation():
    rule = make_raw_rule(kind="trailing_stop", target_price=None, trail_pct=10.0, high_water_mark=None)
    # First ever observation can never be 10% below itself.
    assert evaluate_rule(rule, make_position(), 0.42).should_exit is False


def test_trailing_stop_requires_trail_pct():
    rule = make_raw_rule(kind="trailing_stop", target_price=None, trail_pct=None, high_water_mark=0.9)
    decision = evaluate_rule(rule, make_position(), 0.10)
    assert decision.should_exit is False
    assert "trail_pct" in decision.reason


def test_evaluate_rule_never_mutates_the_rule():
    rule = make_raw_rule(kind="trailing_stop", target_price=None, trail_pct=10.0, high_water_mark=0.60)
    before = json.dumps(rule.to_dict(), sort_keys=True)
    evaluate_rule(rule, make_position(), 0.95)  # a big new high
    evaluate_rule(rule, make_position(), 0.10)  # and a crash
    assert json.dumps(rule.to_dict(), sort_keys=True) == before
    assert rule.high_water_mark == 0.60  # advancing the HWM is the monitor's job


# ------------------------------------------------------------------ time_exit


def test_time_exit_fires_once_the_deadline_passes():
    deadline = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    rule = make_raw_rule(kind="time_exit", target_price=None, expires_at=deadline.isoformat())
    decision = evaluate_rule(rule, make_position(), 0.5, now=deadline + timedelta(seconds=1))
    assert decision.should_exit is True
    assert "Time exit reached" in decision.reason
    # exactly at the deadline counts
    assert evaluate_rule(rule, make_position(), 0.5, now=deadline).should_exit is True


def test_time_exit_does_not_fire_before_the_deadline():
    deadline = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    rule = make_raw_rule(kind="time_exit", target_price=None, expires_at=deadline.isoformat())
    decision = evaluate_rule(rule, make_position(), 0.5, now=deadline - timedelta(minutes=1))
    assert decision.should_exit is False
    assert decision.exit_shares == 0.0


def test_time_exit_accepts_zulu_suffix_and_naive_timestamps():
    deadline = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    for stored in ("2026-01-01T12:00:00Z", "2026-01-01T12:00:00"):
        rule = make_raw_rule(kind="time_exit", target_price=None, expires_at=stored)
        assert evaluate_rule(rule, make_position(), 0.5, now=deadline).should_exit is True


def test_time_exit_without_expiry_cannot_evaluate():
    rule = make_raw_rule(kind="time_exit", target_price=None, expires_at=None)
    assert evaluate_rule(rule, make_position(), 0.5).should_exit is False


# --------------------------------------------------------------------- guards


@pytest.mark.parametrize(
    "overrides,price",
    [
        ({"kind": "take_profit", "target_price": 0.10}, 1.0),
        ({"kind": "stop_loss", "target_price": 0.90}, 0.0001),
        ({"kind": "trailing_stop", "target_price": None, "trail_pct": 5.0, "high_water_mark": 0.99}, 0.1),
        ({"kind": "time_exit", "target_price": None, "expires_at": "2000-01-01T00:00:00Z"}, 0.5),
    ],
)
def test_resolved_position_never_fires(overrides: dict, price: float):
    rule = make_raw_rule(**overrides)
    position = make_position(cur_price=price, redeemable=True, is_resolved=True)
    decision = evaluate_rule(rule, position, price)
    assert decision.should_exit is False
    assert decision.exit_shares == 0.0
    assert "redemption" in decision.reason


def test_inactive_rule_never_fires():
    rule = make_raw_rule(target_price=0.10, active=False)
    decision = evaluate_rule(rule, make_position(), 0.99)
    assert decision.should_exit is False
    assert "inactive" in decision.reason


def test_token_mismatch_refuses_to_act():
    rule = make_raw_rule(token_id="some-other-token", target_price=0.10)
    decision = evaluate_rule(rule, make_position(), 0.99)
    assert decision.should_exit is False
    assert "refusing to act" in decision.reason


def test_empty_position_never_fires():
    rule = make_raw_rule(target_price=0.10)
    decision = evaluate_rule(rule, make_position(shares=0.0), 0.99)
    assert decision.should_exit is False
    assert "no shares" in decision.reason


@pytest.mark.parametrize("price", [0.0, -0.5, 1.5])
def test_bad_price_read_is_refused(price: float):
    rule = make_raw_rule(kind="stop_loss", target_price=0.90)
    decision = evaluate_rule(rule, make_position(), price)
    assert decision.should_exit is False
    assert decision.trigger_price is None
    assert "bad price read" in decision.reason


def test_unknown_kind_is_reported_not_raised():
    rule = make_raw_rule(kind="moon_shot")
    decision = evaluate_rule(rule, make_position(), 0.99)
    assert decision.should_exit is False
    assert "Unknown rule kind" in decision.reason


# ---------------------------------------------------------------- exit_shares


def test_exit_fraction_rounds_down_to_share_precision():
    rule = make_raw_rule(target_price=0.10, exit_fraction=1 / 3)
    decision = evaluate_rule(rule, make_position(shares=10.0), 0.99)
    assert decision.should_exit is True
    assert decision.exit_shares == 3.333333  # floored, never 3.334
    assert decision.exit_shares <= 10.0 / 3


def test_exit_fraction_one_sells_the_whole_holding():
    rule = make_raw_rule(target_price=0.10, exit_fraction=1.0)
    decision = evaluate_rule(rule, make_position(shares=7.123456), 0.99)
    assert decision.exit_shares == 7.123456


def test_dust_exit_is_reported_instead_of_a_zero_size_order():
    rule = make_raw_rule(target_price=0.10, exit_fraction=0.0000001)
    decision = evaluate_rule(rule, make_position(shares=1.0), 0.99)
    assert decision.should_exit is False
    assert decision.exit_shares == 0.0
    assert "nothing to sell" in decision.reason


def test_decisions_are_json_safe():
    decision = evaluate_rule(make_raw_rule(target_price=0.10), make_position(), 0.99)
    assert isinstance(decision, RuleDecision)
    json.dumps(decision.to_dict())  # must not raise


# ------------------------------------------------------------------ make_rule


def test_make_rule_take_profit_derives_target_from_pct():
    rule = make_rule(kind="take_profit", position=make_position(avg_price=0.40), target_pct=25)
    assert rule.kind == "take_profit"
    assert rule.target_price == 0.5
    assert rule.target_pct == 25
    assert rule.token_id == TOKEN
    assert rule.condition_id == COND
    assert rule.outcome == "Yes"
    assert rule.active is True
    assert len(rule.id) == 8
    json.dumps(rule.to_dict())


def test_make_rule_derives_pct_when_given_an_absolute_price():
    rule = make_rule(kind="stop_loss", position=make_position(avg_price=0.50), target_price=0.40)
    assert rule.target_price == 0.40
    assert rule.target_pct == -20.0


def test_make_rule_trailing_stop_seeds_high_water_mark():
    rule = make_rule(
        kind="trailing_stop",
        position=make_position(avg_price=0.40, cur_price=0.65),
        trail_pct=15,
    )
    assert rule.trail_pct == 15
    assert rule.high_water_mark == 0.65  # best of entry and current
    assert rule.target_price is None


def test_make_rule_time_exit_normalises_to_utc_iso():
    later = datetime.now(timezone.utc) + timedelta(days=2)
    rule = make_rule(kind="time_exit", position=make_position(), expires_at=later)
    assert rule.expires_at is not None
    assert rule.expires_at.endswith("+00:00")


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        (dict(kind="nope"), "Unknown rule kind"),
        (dict(kind="take_profit", target_price=0.40), "not above the entry"),
        (dict(kind="take_profit", target_pct=-10), "not above the entry"),
        (dict(kind="take_profit", target_pct=200), "outside (0, 1)"),
        (dict(kind="take_profit"), "needs target_price"),
        (dict(kind="take_profit", target_price=0.7, target_pct=40), "not both"),
        (dict(kind="stop_loss", target_price=0.60), "not below the entry"),
        (dict(kind="stop_loss", target_pct=10), "not below the entry"),
        (dict(kind="trailing_stop"), "needs trail_pct"),
        (dict(kind="trailing_stop", trail_pct=0), "between 0 and 100"),
        (dict(kind="trailing_stop", trail_pct=150), "between 0 and 100"),
        (dict(kind="trailing_stop", trail_pct=10, target_price=0.6), "drop target_price"),
        (dict(kind="time_exit"), "needs expires_at"),
        (dict(kind="time_exit", expires_at="2001-01-01T00:00:00Z"), "in the past"),
        (dict(kind="time_exit", expires_at="not-a-date"), "not a valid ISO"),
        (dict(kind="take_profit", target_price=0.7, exit_fraction=0), "exit_fraction"),
        (dict(kind="take_profit", target_price=0.7, exit_fraction=1.5), "exit_fraction"),
    ],
)
def test_make_rule_rejects_incoherent_input(kwargs: dict, fragment: str):
    with pytest.raises(ValueError) as excinfo:
        make_rule(position=make_position(avg_price=0.50), **kwargs)
    assert fragment in str(excinfo.value)


def test_make_rule_refuses_resolved_or_empty_positions():
    with pytest.raises(ValueError, match="already resolved"):
        make_rule(
            kind="take_profit",
            position=make_position(cur_price=1.0, redeemable=True, is_resolved=True),
            target_price=0.9,
        )
    with pytest.raises(ValueError, match="no shares"):
        make_rule(kind="take_profit", position=make_position(shares=0.0), target_price=0.9)


def test_rules_from_make_rule_evaluate_as_expected():
    position = make_position(avg_price=0.50, cur_price=0.55)
    take_profit = make_rule(kind="take_profit", position=position, target_pct=20)  # -> 0.60
    assert evaluate_rule(take_profit, position, 0.59).should_exit is False
    assert evaluate_rule(take_profit, position, 0.61).should_exit is True

    stop = make_rule(kind="stop_loss", position=position, target_pct=-20)  # -> 0.40
    assert evaluate_rule(stop, position, 0.41).should_exit is False
    assert evaluate_rule(stop, position, 0.39).should_exit is True


# ------------------------------------------------------------------ RuleStore


def test_store_is_empty_when_the_file_does_not_exist(settings: Settings):
    store = RuleStore(settings)
    assert store.list() == []
    assert store.get("nope") is None
    assert store.remove("nope") is False
    assert not store.path.exists()  # reading must not create anything


def test_store_round_trip_preserves_every_field(settings: Settings):
    store = RuleStore(settings)
    rule = make_rule(
        kind="trailing_stop",
        position=make_position(avg_price=0.40, cur_price=0.62),
        trail_pct=12.5,
        exit_fraction=0.5,
        note="lock in some of the run",
    )
    store.add(rule)

    loaded = store.get(rule.id)
    assert loaded is not None
    assert loaded.to_dict() == rule.to_dict()
    assert [r.id for r in store.list()] == [rule.id]
    assert store.path.exists()

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["rules"]) == 1


def test_store_rejects_duplicate_ids(settings: Settings):
    store = RuleStore(settings)
    rule = make_rule(kind="take_profit", position=make_position(), target_price=0.7)
    store.add(rule)
    with pytest.raises(ValueError, match="already exists"):
        store.add(rule)


def test_store_update_persists_changes(settings: Settings):
    store = RuleStore(settings)
    rule = store.add(make_rule(kind="trailing_stop", position=make_position(), trail_pct=10))

    rule.high_water_mark = 0.88  # what the monitor does after a new high
    rule.active = False
    store.update(rule)

    reloaded = store.get(rule.id)
    assert reloaded is not None
    assert reloaded.high_water_mark == 0.88
    assert reloaded.active is False
    assert store.list(active_only=True) == []
    assert len(store.list()) == 1


def test_store_update_unknown_rule_raises(settings: Settings):
    store = RuleStore(settings)
    orphan = make_rule(kind="take_profit", position=make_position(), target_price=0.7)
    with pytest.raises(ValueError, match="No rule with id"):
        store.update(orphan)


def test_store_remove_and_for_token(settings: Settings):
    store = RuleStore(settings)
    first = store.add(make_rule(kind="take_profit", position=make_position(), target_price=0.7))
    other_position = make_position(token_id="token-other", condition_id="0xother")
    second = store.add(make_rule(kind="stop_loss", position=other_position, target_price=0.3))

    assert [r.id for r in store.for_token(TOKEN)] == [first.id]
    assert [r.id for r in store.for_token("token-other")] == [second.id]
    assert store.for_token("missing") == []

    assert store.remove(first.id) is True
    assert store.remove(first.id) is False
    assert [r.id for r in store.list()] == [second.id]


def test_store_survives_a_corrupt_file(settings: Settings):
    store = RuleStore(settings)
    settings.ensure_data_dir()
    store.path.write_text("{not json at all", encoding="utf-8")

    assert store.list() == []  # treated as empty rather than crashing
    assert store.path.with_name(store.path.name + ".corrupt").exists()

    rule = store.add(make_rule(kind="take_profit", position=make_position(), target_price=0.7))
    assert [r.id for r in store.list()] == [rule.id]


def test_store_skips_unreadable_entries_but_keeps_the_rest(settings: Settings):
    store = RuleStore(settings)
    settings.ensure_data_dir()
    good = make_rule(kind="take_profit", position=make_position(), target_price=0.7)
    store.path.write_text(
        json.dumps({"version": 1, "rules": [{"no_id": True}, good.to_dict(), "garbage"]}),
        encoding="utf-8",
    )
    assert [r.id for r in store.list()] == [good.id]


def test_store_accepts_a_bare_list_file(settings: Settings):
    store = RuleStore(settings)
    settings.ensure_data_dir()
    rule = make_rule(kind="take_profit", position=make_position(), target_price=0.7)
    store.path.write_text(json.dumps([rule.to_dict()]), encoding="utf-8")
    assert [r.id for r in store.list()] == [rule.id]


def test_store_writes_leave_no_temp_files_behind(settings: Settings):
    store = RuleStore(settings)
    store.add(make_rule(kind="take_profit", position=make_position(), target_price=0.7))
    store.add(make_rule(kind="stop_loss", position=make_position(), target_price=0.3))
    assert list(settings.data_dir.glob("*.tmp")) == []
    assert list(settings.data_dir.glob("*.lock")) == []  # the lock is always released


# ------------------------------------------- a blocked read is not corruption


def test_transient_os_error_raises_instead_of_destroying_the_store(settings: Settings, monkeypatch):
    """A sharing violation (antivirus/OneDrive/backup holding the file open) is
    transient and the file is intact. Quarantining it would rename every armed
    stop-loss out of the way and let the monitor report a clean pass."""
    store = RuleStore(settings)
    rule = store.add(make_rule(kind="stop_loss", position=make_position(), target_price=0.3))
    corrupt = store.path.with_name(store.path.name + ".corrupt")

    real_read_text = Path.read_text

    def blocked(self: Path, *args, **kwargs):
        if self == store.path:
            raise PermissionError(13, "The process cannot access the file: used by another process")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked)
    with pytest.raises(OSError):
        store.list(active_only=True)
    monkeypatch.undo()

    assert not corrupt.exists()  # the intact file stayed exactly where it was
    assert [r.id for r in store.list()] == [rule.id]  # still armed


def test_missing_file_is_empty_not_an_error_even_when_it_vanishes_mid_read(settings: Settings, monkeypatch):
    store = RuleStore(settings)
    store.add(make_rule(kind="stop_loss", position=make_position(), target_price=0.3))

    real_read_text = Path.read_text

    def vanished(self: Path, *args, **kwargs):
        if self == store.path:
            raise FileNotFoundError(2, "No such file or directory")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", vanished)
    assert store.list() == []  # absent store, not a failure


def test_non_utf8_content_is_still_quarantined(settings: Settings):
    store = RuleStore(settings)
    settings.ensure_data_dir()
    store.path.write_bytes(b"\xff\xfe\x00\x01 not text at all")

    assert store.list() == []
    assert store.path.with_name(store.path.name + ".corrupt").exists()


# ---------------------------------------------- stored exit_fraction is trusted


@pytest.mark.parametrize("stored", [0.0, 0, "0", -0.5, 1.5, 100])
def test_from_dict_rejects_an_out_of_range_exit_fraction(stored: object):
    data = make_raw_rule(exit_fraction=1.0).to_dict()
    data["exit_fraction"] = stored
    with pytest.raises(ValueError, match="exit_fraction"):
        ExitRule.from_dict(data)


@pytest.mark.parametrize("stored", [None, ""])
def test_from_dict_treats_an_absent_exit_fraction_as_sell_all(stored: object):
    data = make_raw_rule(exit_fraction=1.0).to_dict()
    data["exit_fraction"] = stored
    assert ExitRule.from_dict(data).exit_fraction == 1.0

    data.pop("exit_fraction")
    assert ExitRule.from_dict(data).exit_fraction == 1.0


def test_from_dict_keeps_a_legitimate_partial_exit():
    data = make_raw_rule(exit_fraction=0.25).to_dict()
    assert ExitRule.from_dict(data).exit_fraction == 0.25


def test_store_drops_a_zero_fraction_rule_instead_of_selling_everything(settings: Settings):
    """A stored "sell 0%" must never load as "sell 100%"."""
    store = RuleStore(settings)
    settings.ensure_data_dir()
    zero = make_raw_rule(id="zerofrac", kind="stop_loss", target_price=0.35, exit_fraction=0.0).to_dict()
    good = make_raw_rule(id="goodfrac", kind="stop_loss", target_price=0.35, exit_fraction=0.25).to_dict()
    store.path.write_text(json.dumps({"version": 1, "rules": [zero, good]}), encoding="utf-8")

    loaded = {r.id: r.exit_fraction for r in store.list()}
    assert loaded == {"goodfrac": 0.25}  # the zero-fraction rule is inert, not inflated

    # Rejected is not deleted: an unrelated mutation carries it through untouched.
    store.add(make_rule(kind="take_profit", position=make_position(), target_price=0.7))
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))["rules"]
    assert [r for r in on_disk if r["id"] == "zerofrac"] == [zero]

    # ...and the owner can still get rid of it.
    assert store.remove("zerofrac") is True
    assert [r["id"] for r in json.loads(store.path.read_text(encoding="utf-8"))["rules"] if r["id"] == "zerofrac"] == []


# ------------------------------------------------------------ concurrent writes


class SlowReadStore(RuleStore):
    """Widens the read->mutate->write window so an interleaving is deterministic."""

    def _read_payload(self):
        payload = super()._read_payload()
        time.sleep(0.15)
        return payload


def test_concurrent_writers_do_not_lose_or_resurrect_a_rule(settings: Settings):
    """The monitor retiring a filled rule and the CLI adding one at the same
    moment. Without a lock the retired rule comes back and gets sold twice."""
    setup = RuleStore(settings)
    setup.add(make_raw_rule(id="keep0001", kind="stop_loss", target_price=0.3))
    setup.add(make_raw_rule(id="filled01", kind="take_profit", target_price=0.7))

    errors: list[str] = []

    def retire() -> None:  # monitor: the position was sold, drop the rule
        try:
            SlowReadStore(settings).remove("filled01")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"retire: {type(exc).__name__}: {exc}")

    def add_new() -> None:  # owner via CLI/Telegram, at the same moment
        try:
            SlowReadStore(settings).add(make_raw_rule(id="newrule1", kind="stop_loss", target_price=0.2))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"add: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=retire), threading.Thread(target=add_new)]
    for thread in threads:
        thread.start()
        time.sleep(0.02)  # make the overlap reproducible
    for thread in threads:
        thread.join()

    assert errors == []
    final = sorted(r.id for r in RuleStore(settings).list())
    assert final == ["keep0001", "newrule1"]  # not lost, and filled01 not resurrected
    assert not RuleStore(settings).lock_path.exists()


def test_a_held_lock_blocks_a_mutation_instead_of_clobbering_it(settings: Settings, monkeypatch):
    store = RuleStore(settings)
    rule = store.add(make_raw_rule(id="keep0001", kind="stop_loss", target_price=0.3))
    before = store.path.read_text(encoding="utf-8")

    monkeypatch.setattr(rules_module, "_LOCK_TIMEOUT_SECONDS", 0.2)
    store.lock_path.write_text("held by a live process", encoding="utf-8")  # fresh => not stale
    try:
        with pytest.raises(TimeoutError):
            store.add(make_raw_rule(id="newrule1", kind="stop_loss", target_price=0.2))
    finally:
        store.lock_path.unlink()

    assert store.path.read_text(encoding="utf-8") == before  # nothing was changed
    assert [r.id for r in store.list()] == [rule.id]


def test_a_stale_lock_from_a_dead_process_is_broken(settings: Settings):
    """A process killed mid-write must not disable rule edits forever."""
    store = RuleStore(settings)
    store.add(make_raw_rule(id="keep0001", kind="stop_loss", target_price=0.3))

    store.lock_path.write_text("left behind by a process that died", encoding="utf-8")
    dead_long_ago = time.time() - (rules_module._LOCK_STALE_SECONDS + 5)
    os.utime(store.lock_path, (dead_long_ago, dead_long_ago))

    store.add(make_raw_rule(id="newrule1", kind="stop_loss", target_price=0.2))
    assert sorted(r.id for r in store.list()) == ["keep0001", "newrule1"]
    assert not store.lock_path.exists()
    assert list(settings.data_dir.glob("*.stale")) == []


def test_reads_do_not_need_the_lock(settings: Settings):
    """Reads stay lock-free (os.replace already gives them a whole file), so a
    monitor sweep can never be blocked out by a busy writer."""
    store = RuleStore(settings)
    rule = store.add(make_raw_rule(id="keep0001", kind="stop_loss", target_price=0.3))

    store.lock_path.write_text("someone is writing", encoding="utf-8")
    try:
        assert [r.id for r in store.list()] == [rule.id]
        assert store.get(rule.id) is not None
        assert [r.id for r in store.for_token(TOKEN)] == [rule.id]
    finally:
        store.lock_path.unlink()
