"""Exit rules: take-profit, stop-loss, trailing stop and time exit.

A rule is a stored *intention*, not a prediction. Nothing here forecasts a
market or implies that an exit will be profitable; it only says "when the
observed price (or the clock) reaches this level, sell this much".

What actually enforces a rule matters, so be honest about it:

  - **take_profit** is the only kind that can also exist on the exchange, as a
    resting SELL limit order. That survives the bot being offline.
  - **stop_loss / trailing_stop / time_exit** have no Polymarket equivalent.
    They exist only in this file plus our polling monitor. If the monitor is
    not running, they protect nothing.

`evaluate_rule` is a pure function of `(rule, position, price)`: no I/O, no
clock other than the one you pass in, and it never mutates the rule. Advancing
`high_water_mark` and persisting it belongs to the monitor, which owns the
store. That split is what makes the monitor testable and stops a rule from
being silently changed just by being read.

`RuleStore` is read and written by two processes at once (the long-running
monitor, plus whatever the owner drives by hand), so it takes an exclusive lock
around every mutation and is careful to tell "this file is damaged" apart from
"this file is momentarily unreadable". Both halves protect the same thing: a
stop-loss must never disappear, or come back, without somebody being told.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from polymarket_bot.config import Settings
from polymarket_bot.portfolio import PositionView

RuleKind = Literal["take_profit", "stop_loss", "trailing_stop", "time_exit"]

VALID_KINDS: tuple[str, ...] = ("take_profit", "stop_loss", "trailing_stop", "time_exit")

# Shares are 6-decimal fixed point on-chain; never ask to sell finer than that.
_SHARE_STEP = Decimal("0.000001")

_STORE_VERSION = 1

# ---- store locking ---------------------------------------------------------
# Every mutation is "read the whole file, change one rule, write the whole
# file". Two of those interleaving loses a write, and can RESURRECT a rule the
# monitor just retired after a fill - which would sell the same position a
# second time. The monitor and the CLI/Telegram side are separate processes, so
# the lock has to be one too: an O_CREAT|O_EXCL file next to the store.
_LOCK_TIMEOUT_SECONDS = 10.0  # give up waiting, change nothing, and say so
_LOCK_POLL_SECONDS = 0.02  # how often a waiter retries
_LOCK_STALE_SECONDS = 30.0  # older than this is assumed dead; see _break_stale_lock
# Windows lets a reader (or a virus scanner) block a rename with a sharing
# violation, so the final os.replace gets a few short retries before it gives up.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.05


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _price(value: float) -> float:
    return round(float(value), 6)


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)  # type: ignore[arg-type]


def _exit_fraction(value: object) -> float:
    """Parse a stored `exit_fraction`, refusing anything outside (0, 1].

    Absent (or empty) means "sell the whole holding" - that is the documented
    default. A stored 0.0 does NOT: it is a rule that says "sell nothing", and
    `float(value or 1.0)` would turn it into "sell everything", i.e. liquidate
    the position the rule was meant to leave alone. There is no safe coercion
    for an out-of-range fraction either, so this raises and the entry is
    rejected on load rather than acted on.
    """
    if value is None or value == "":
        return 1.0
    fraction = float(value)  # type: ignore[arg-type]
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"exit_fraction must be in (0, 1], got {value!r}")
    return fraction


def _to_utc(value: str | datetime) -> datetime:
    """ISO string or datetime -> aware UTC datetime. Raises ValueError if unparseable."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    # Naive timestamps are treated as UTC: everything the API hands us is UTC,
    # and guessing local time would silently shift a deadline.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _floor_shares(shares: float, fraction: float) -> float:
    """`shares * fraction`, rounded DOWN to on-chain share precision.

    Done in Decimal so 10 * 0.333 is 3.33 and not 3.329999..., and rounded down
    so a partial exit can never ask for more than the account holds (an
    over-sized SELL is simply rejected by the CLOB).
    """
    if shares <= 0 or fraction <= 0:
        return 0.0
    raw = Decimal(str(shares)) * Decimal(str(min(fraction, 1.0)))
    return float(raw.quantize(_SHARE_STEP, rounding=ROUND_DOWN))


def _replace_with_retry(src: Path, dst: Path) -> None:
    """`os.replace(src, dst)`, retried briefly before giving up.

    On Windows a reader holding the target open (another process listing rules,
    a backup agent, a virus scanner) makes the rename fail with a sharing
    violation even though nothing is actually wrong. A few short retries turn
    that into a small delay; a persistent failure still raises, because a write
    that quietly did not happen is exactly the kind of bug this module exists
    to avoid.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS)


@dataclass
class ExitRule:
    """One stored exit instruction attached to a specific outcome token."""

    id: str
    condition_id: str
    token_id: str
    market_title: str
    outcome: str
    kind: RuleKind
    target_price: float | None = None  # absolute price trigger
    target_pct: float | None = None  # +25 => +25% vs avg entry; -20 => -20%
    trail_pct: float | None = None  # trailing_stop only
    exit_fraction: float = 1.0  # portion of the holding to exit
    high_water_mark: float | None = None  # best price seen; monitor maintains it
    expires_at: str | None = None  # ISO; time_exit fires at/after this
    active: bool = True
    created_at: str = field(default_factory=_now_iso)
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "market_title": self.market_title,
            "outcome": self.outcome,
            "kind": self.kind,
            "target_price": self.target_price,
            "target_pct": self.target_pct,
            "trail_pct": self.trail_pct,
            "exit_fraction": self.exit_fraction,
            "high_water_mark": self.high_water_mark,
            "expires_at": self.expires_at,
            "active": self.active,
            "created_at": self.created_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ExitRule:
        """Rebuild from stored JSON. Tolerant of missing optional keys.

        Tolerant is not the same as forgiving: a value that is present but
        outside its legal range raises, because a rule that has to be guessed
        at is a rule that can fire the wrong way. `RuleStore` keeps such an
        entry on disk untouched instead of acting on it.
        """
        return cls(
            id=str(data["id"]),
            condition_id=str(data.get("condition_id") or ""),
            token_id=str(data.get("token_id") or ""),
            market_title=str(data.get("market_title") or ""),
            outcome=str(data.get("outcome") or ""),
            kind=str(data.get("kind") or ""),  # type: ignore[arg-type]
            target_price=_opt_float(data.get("target_price")),
            target_pct=_opt_float(data.get("target_pct")),
            trail_pct=_opt_float(data.get("trail_pct")),
            exit_fraction=_exit_fraction(data.get("exit_fraction")),
            high_water_mark=_opt_float(data.get("high_water_mark")),
            expires_at=str(data["expires_at"]) if data.get("expires_at") else None,
            active=bool(data.get("active", True)),
            created_at=str(data.get("created_at") or _now_iso()),
            note=str(data["note"]) if data.get("note") else None,
        )


@dataclass
class RuleDecision:
    """The verdict for one rule against one live price observation."""

    should_exit: bool
    reason: str
    exit_shares: float  # 0.0 whenever should_exit is False
    trigger_price: float | None  # the price the decision was made on
    rule_id: str

    def to_dict(self) -> dict:
        return {
            "should_exit": self.should_exit,
            "reason": self.reason,
            "exit_shares": self.exit_shares,
            "trigger_price": self.trigger_price,
            "rule_id": self.rule_id,
        }


class RuleStore:
    """Rules persisted as JSON at `settings.rules_path`.

    Every write goes to a temp file in the same directory and is moved into
    place with `os.replace`, so a crash mid-write leaves the previous file
    intact rather than a half-written one, and a reader always sees a complete
    file (which is why reads take no lock). Mutations do take one: see
    `_locked`.

    What a read does with a bad file matters more than it looks:

      - **Missing file** -> no rules. Not an error; nothing has been stored yet.
      - **Unparseable content** (not JSON, or not UTF-8) -> the file is moved
        aside as `<name>.corrupt` and the store reads as empty. That is real
        corruption, and there is nothing to recover.
      - **Any other OSError** (a Windows sharing violation from a backup agent
        holding the file open, a permissions problem, a disconnected drive)
        -> **raises**. It is transient and the file is intact, so quarantining
        it would rename a perfectly good set of armed stop-losses out of the
        way and report "0 rules, all clear" to a monitor that has no way to
        tell the difference. Callers must be able to see the failure.
      - **An entry this version cannot read** -> skipped, and carried through
        the next write untouched, so an unrelated `add` cannot delete it.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def path(self) -> Path:
        return self.settings.rules_path

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    # ---- locking -----------------------------------------------------

    def _break_stale_lock(self) -> bool:
        """Drop a lock old enough that its holder must be dead.

        Staleness policy: holding the lock means reading and rewriting one
        small JSON file - single-digit milliseconds. A lock older than
        `_LOCK_STALE_SECONDS` (30s) therefore means the holder died mid-write
        (crash, kill, power loss) rather than that it is still working, so the
        lock is broken rather than waited on. Nothing deadlocks permanently.

        The break itself is a rename to a unique name: only the process whose
        rename succeeds has actually broken the lock, so two waiters can never
        both decide they won and then both mutate.

        Returns True when the lock is gone and acquiring is worth retrying at
        once, False when it is still held by someone alive.
        """
        lock_path = self.lock_path
        try:
            age = time.time() - os.stat(lock_path).st_mtime
        except FileNotFoundError:
            return True  # released while we looked; try again immediately
        except OSError:
            return False
        if age < _LOCK_STALE_SECONDS:
            return False
        victim = lock_path.with_name(f"{lock_path.name}.{os.getpid()}-{uuid4().hex[:6]}.stale")
        try:
            os.rename(lock_path, victim)
        except OSError:
            return False  # someone else broke or released it first
        try:
            os.unlink(victim)
        except OSError:
            pass
        return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive cross-process lock for one load -> mutate -> write.

        `os.open(..., O_CREAT | O_EXCL)` is atomic on Windows and POSIX alike
        and needs no third-party package. Waiting is bounded: after
        `_LOCK_TIMEOUT_SECONDS` this raises `TimeoutError` and the caller's
        mutation simply does not happen, which is the safe direction - a failed
        write that says so beats a successful write that silently discards
        somebody else's.
        """
        self.settings.ensure_data_dir()
        lock_path = self.lock_path
        stamp = json.dumps({"pid": os.getpid(), "acquired_at": _now_iso()}).encode("ascii")
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS

        while True:
            # Deadline is checked once per iteration, before the retry - not
            # only on the "someone else holds it" branch. _break_stale_lock
            # returns True on FileNotFoundError ("it vanished while we looked"),
            # and that branch used to `continue` with no deadline check and no
            # sleep: under create/delete contention the loop span ~118,000
            # iterations in 1.5s, pegging a core inside the monitor sweep and
            # never timing out.
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Could not lock {lock_path} within {_LOCK_TIMEOUT_SECONDS:g}s - "
                    f"another process is writing rules. Nothing was changed."
                )
            try:
                handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self._break_stale_lock()
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            # From here the lock file EXISTS and is ours. Any failure before the
            # yield below must remove it, or a failed stamp write orphans the
            # lock and blocks every rule edit until it goes stale (30s).
            try:
                os.write(handle, stamp)
            except BaseException:
                os.close(handle)
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                raise
            os.close(handle)
            break

        try:
            yield
        finally:
            try:
                os.unlink(lock_path)
            except OSError:
                pass

    # ---- persistence -------------------------------------------------

    def _quarantine(self) -> None:
        # Keep exactly one copy; a corrupt file that keeps failing must not
        # spawn a new backup on every read. If even this fails the corrupt file
        # stays where it is - the read still reports empty, and the next write
        # will fail on the same underlying problem rather than overwrite it.
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
        except OSError:
            pass

    def _read_payload(self) -> tuple[list[ExitRule], list[dict]]:
        """Return `(rules, unreadable_entries)` from disk.

        Only genuine content corruption is quarantined. Every other OSError
        propagates: see the class docstring for why that distinction is the
        whole point of this method.
        """
        path = self.path
        if not path.exists():
            return [], []
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Raced with a delete between the check and the read. An absent
            # store is an empty store, not a failure.
            return [], []
        except ValueError:
            # UnicodeDecodeError: the bytes are not UTF-8, so the content is
            # damaged rather than the access being blocked.
            self._quarantine()
            return [], []
        # NOTE: OSError is deliberately NOT caught here.

        try:
            raw = json.loads(text)
        except ValueError:
            self._quarantine()
            return [], []

        if isinstance(raw, dict):
            items = raw.get("rules") or []
        elif isinstance(raw, list):  # tolerate a bare list from an older format
            items = raw
        else:
            items = []

        rules: list[ExitRule] = []
        unreadable: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                rules.append(ExitRule.from_dict(item))
            except Exception:  # one bad entry must not hide every other rule
                unreadable.append(item)
        return rules, unreadable

    def _load(self) -> list[ExitRule]:
        rules, _ = self._read_payload()
        return rules

    def _write(self, rules: list[ExitRule], unreadable: list[dict] | None = None) -> None:
        self.settings.ensure_data_dir()
        stored = [r.to_dict() for r in rules]
        # Entries this version could not parse ride along untouched: refusing
        # to act on a rule is right, deleting it because of an unrelated edit
        # is not.
        stored.extend(unreadable or [])
        payload = {
            "version": _STORE_VERSION,
            "updated_at": _now_iso(),
            "rules": stored,
        }
        # Same directory as the target, otherwise os.replace can cross volumes
        # and stop being atomic.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}-{uuid4().hex[:6]}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(tmp, self.path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    # ---- API ---------------------------------------------------------

    def list(self, *, active_only: bool = False) -> list[ExitRule]:
        rules = self._load()
        if active_only:
            return [r for r in rules if r.active]
        return rules

    def add(self, rule: ExitRule) -> ExitRule:
        with self._locked():
            rules, unreadable = self._read_payload()
            # Unreadable entries count too: they are still written back, and two
            # entries sharing an id would make `get` ambiguous.
            if any(r.id == rule.id for r in rules) or any(str(d.get("id")) == rule.id for d in unreadable):
                raise ValueError(f"A rule with id {rule.id!r} already exists")
            rules.append(rule)
            self._write(rules, unreadable)
        return rule

    def get(self, rule_id: str) -> ExitRule | None:
        for rule in self._load():
            if rule.id == rule_id:
                return rule
        return None

    def update(self, rule: ExitRule) -> ExitRule:
        with self._locked():
            rules, unreadable = self._read_payload()
            for index, existing in enumerate(rules):
                if existing.id == rule.id:
                    rules[index] = rule
                    self._write(rules, unreadable)
                    return rule
        raise ValueError(f"No rule with id {rule.id!r}")

    def remove(self, rule_id: str) -> bool:
        wanted = str(rule_id)
        with self._locked():
            rules, unreadable = self._read_payload()
            kept = [r for r in rules if r.id != wanted]
            # Removing an entry that cannot be parsed has to work too, or a
            # rejected rule would be stuck on disk with no way to clear it.
            kept_unreadable = [d for d in unreadable if str(d.get("id")) != wanted]
            if len(kept) == len(rules) and len(kept_unreadable) == len(unreadable):
                return False
            self._write(kept, kept_unreadable)
        return True

    def for_token(self, token_id: str) -> list[ExitRule]:
        wanted = str(token_id)
        return [r for r in self._load() if r.token_id == wanted]


def _resolved_target(rule: ExitRule, position: PositionView) -> float | None:
    """Absolute trigger price for take_profit/stop_loss, or None if unset."""
    if rule.target_price is not None:
        return float(rule.target_price)
    if rule.target_pct is not None and position.avg_price > 0:
        return position.avg_price * (1 + float(rule.target_pct) / 100.0)
    return None


def _no_target_reason(rule: ExitRule, position: PositionView) -> str:
    """Why a percentage rule could not produce a trigger price.

    Distinguishes "the rule is malformed" from "we do not know the entry price
    yet" - the second happens for a few seconds after a fill, while the data
    API still reports avgPrice as 0, and telling the user their rule has no
    target would send them looking for a bug that is not there.
    """
    if rule.target_pct is not None and position.avg_price <= 0:
        return (
            f"{rule.kind} is set to {rule.target_pct:+g}% of entry, but the entry price is not "
            f"known yet (the exchange reports avg price 0, which it does briefly after a fill). "
            f"Holding off rather than measuring a percentage against zero."
        )
    return f"{rule.kind} rule has neither target_price nor target_pct - cannot evaluate."


def evaluate_rule(
    rule: ExitRule,
    position: PositionView,
    current_price: float,
    *,
    now: datetime | None = None,
) -> RuleDecision:
    """Decide whether `rule` fires for `position` at `current_price`.

    Pure: reads nothing, writes nothing, and never mutates `rule` — including
    `high_water_mark`, which the monitor advances and persists itself. `now` is
    injectable so time exits stay deterministic in tests.

    `exit_shares` is 0.0 whenever `should_exit` is False, so a caller that
    forgets to check the flag still cannot sell anything.
    """
    shares = _floor_shares(position.shares, rule.exit_fraction)

    try:
        price = float(current_price)
    except (TypeError, ValueError):
        price = float("nan")
    # A live outcome token trades in (0, 1]. Anything else is a bad read (or a
    # market that already settled) and must not be traded on.
    observed: float | None = price if 0.0 < price <= 1.0 else None

    def verdict(should_exit: bool, reason: str) -> RuleDecision:
        if should_exit and shares <= 0:
            # The trigger is real but there is nothing sellable; report the
            # facts instead of emitting a zero-size order.
            return RuleDecision(
                should_exit=False,
                reason=f"{reason} But {rule.exit_fraction:g} of {position.shares:g} shares rounds to 0 — nothing to sell.",
                exit_shares=0.0,
                trigger_price=observed,
                rule_id=rule.id,
            )
        return RuleDecision(
            should_exit=should_exit,
            reason=reason,
            exit_shares=shares if should_exit else 0.0,
            trigger_price=observed,
            rule_id=rule.id,
        )

    if not rule.active:
        return verdict(False, "Rule is inactive.")

    if rule.token_id and position.token_id and rule.token_id != position.token_id:
        return verdict(
            False,
            f"Rule targets token {rule.token_id} but was evaluated against {position.token_id} — refusing to act.",
        )

    if position.is_resolved:
        # Settled markets pay out through redemption; there is no order book
        # left to sell into, so an exit rule must never fire here.
        return verdict(
            False,
            "Market has resolved — the exit is redemption, not a sale, so this rule will not fire.",
        )

    if position.shares <= 0:
        return verdict(False, "Position holds no shares.")

    bad_price = f"Current price {current_price!r} is outside 0..1 — refusing to act on a bad price read."

    if rule.kind == "take_profit":
        target = _resolved_target(rule, position)
        if target is None:
            return verdict(False, _no_target_reason(rule, position))
        if observed is None:
            return verdict(False, bad_price)
        if observed >= target:
            return verdict(
                True,
                f"Take-profit hit: price {observed:.4f} >= target {target:.4f} (entry {position.avg_price:.4f}).",
            )
        return verdict(
            False,
            f"Take-profit not hit: price {observed:.4f} < target {target:.4f}.",
        )

    if rule.kind == "stop_loss":
        target = _resolved_target(rule, position)
        if target is None:
            return verdict(False, _no_target_reason(rule, position))
        if observed is None:
            return verdict(False, bad_price)
        if observed <= target:
            return verdict(
                True,
                f"Stop-loss hit: price {observed:.4f} <= target {target:.4f} (entry {position.avg_price:.4f}).",
            )
        return verdict(
            False,
            f"Stop-loss not hit: price {observed:.4f} > target {target:.4f}.",
        )

    if rule.kind == "trailing_stop":
        if rule.trail_pct is None or rule.trail_pct <= 0:
            return verdict(False, "trailing_stop rule has no usable trail_pct — cannot evaluate.")
        if observed is None:
            return verdict(False, bad_price)
        # Fold the current observation into the high-water mark so the verdict
        # is correct whether or not the monitor has persisted the new high yet.
        # This is a local value; rule.high_water_mark is left untouched.
        stored = rule.high_water_mark if rule.high_water_mark is not None else 0.0
        hwm = max(float(stored), observed)
        stop = hwm * (1 - float(rule.trail_pct) / 100.0)
        if observed <= stop:
            return verdict(
                True,
                f"Trailing stop hit: price {observed:.4f} <= {stop:.4f} "
                f"({rule.trail_pct:g}% below the {hwm:.4f} high).",
            )
        return verdict(
            False,
            f"Trailing stop not hit: price {observed:.4f} > {stop:.4f} "
            f"({rule.trail_pct:g}% below the {hwm:.4f} high).",
        )

    if rule.kind == "time_exit":
        if not rule.expires_at:
            return verdict(False, "time_exit rule has no expires_at — cannot evaluate.")
        try:
            deadline = _to_utc(rule.expires_at)
        except ValueError:
            return verdict(False, f"time_exit expires_at {rule.expires_at!r} is not a valid ISO timestamp.")
        moment = _to_utc(now) if now is not None else _now()
        if moment >= deadline:
            return verdict(
                True,
                f"Time exit reached: deadline {deadline.isoformat(timespec='seconds')} "
                f"passed (now {moment.isoformat(timespec='seconds')}).",
            )
        return verdict(
            False,
            f"Time exit not reached: deadline {deadline.isoformat(timespec='seconds')} "
            f"is still ahead (now {moment.isoformat(timespec='seconds')}).",
        )

    return verdict(False, f"Unknown rule kind {rule.kind!r} — cannot evaluate.")


def make_rule(
    *,
    kind: RuleKind,
    position: PositionView,
    target_price: float | None = None,
    target_pct: float | None = None,
    trail_pct: float | None = None,
    exit_fraction: float = 1.0,
    expires_at: str | datetime | None = None,
    note: str | None = None,
) -> ExitRule:
    """Build a validated rule for `position`, deriving what it can from it.

    Raises ValueError on anything incoherent (a take-profit below entry, a stop
    above entry, a trailing stop without a trail, a time exit without or with a
    past deadline). Rejecting here is deliberate: a rule that can only ever fire
    the wrong way is worse than no rule.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"Unknown rule kind {kind!r}; expected one of {', '.join(VALID_KINDS)}")
    if not position.token_id:
        raise ValueError("Position has no token_id, so there is nothing a rule could sell")
    if position.shares <= 0:
        raise ValueError("Cannot attach an exit rule to a position with no shares")
    if position.is_resolved:
        raise ValueError(
            "That market has already resolved — the exit is redemption, not a sale, so an exit rule would never fire"
        )

    fraction = float(exit_fraction)
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"exit_fraction must be in (0, 1], got {exit_fraction!r}")

    entry = float(position.avg_price)
    current = float(position.cur_price)

    resolved_price: float | None = None
    resolved_pct: float | None = None
    resolved_trail: float | None = None
    expires_iso: str | None = None
    hwm: float | None = None

    if kind in ("take_profit", "stop_loss"):
        if trail_pct is not None:
            raise ValueError(f"trail_pct only applies to trailing_stop, not {kind}")
        if expires_at is not None:
            raise ValueError(f"expires_at only applies to time_exit, not {kind}")
        if target_price is None and target_pct is None:
            raise ValueError(f"{kind} needs target_price (absolute) or target_pct (percent vs entry)")
        if target_price is not None and target_pct is not None:
            raise ValueError(f"{kind}: pass target_price or target_pct, not both")

        if target_price is not None:
            target = float(target_price)
        else:
            if entry <= 0:
                raise ValueError(
                    "Position has no average entry price, so target_pct cannot be resolved — pass target_price instead"
                )
            target = entry * (1 + float(target_pct) / 100.0)  # type: ignore[arg-type]

        if not 0.0 < target < 1.0:
            raise ValueError(
                f"Resolved target price {target:.4f} is outside (0, 1) — an outcome share can never be worth "
                "more than $1.00 or less than $0.00"
            )
        if kind == "take_profit" and target <= entry:
            raise ValueError(
                f"take_profit target {target:.4f} is not above the entry price {entry:.4f} — that would lock in "
                "a loss; use stop_loss if that is what you mean"
            )
        if kind == "stop_loss" and target >= entry:
            raise ValueError(
                f"stop_loss target {target:.4f} is not below the entry price {entry:.4f}; to protect a gain that "
                "is already on the table use trailing_stop instead"
            )

        resolved_price = _price(target)
        # Store both representations: the absolute price is what fires, the
        # percentage is what the owner asked for and reads back sensibly.
        if target_pct is not None:
            resolved_pct = round(float(target_pct), 4)
        elif entry > 0:
            resolved_pct = round((target / entry - 1) * 100.0, 4)

    elif kind == "trailing_stop":
        if target_price is not None or target_pct is not None:
            raise ValueError("trailing_stop is driven by trail_pct alone — drop target_price/target_pct")
        if expires_at is not None:
            raise ValueError("expires_at only applies to time_exit, not trailing_stop")
        if trail_pct is None:
            raise ValueError("trailing_stop needs trail_pct (e.g. trail_pct=10 exits 10% below the highest price seen)")
        trail = float(trail_pct)
        if not 0.0 < trail < 100.0:
            raise ValueError(f"trail_pct must be between 0 and 100 (exclusive), got {trail_pct!r}")
        resolved_trail = trail
        # No price history is available here, and the position provably traded
        # at its entry price, so seed the high-water mark with the better of
        # entry and current. A holding already more than trail_pct below entry
        # therefore triggers on the first check — intended, not a bug.
        seed = max(entry, current)
        hwm = _price(seed) if seed > 0 else None

    else:  # time_exit
        if target_price is not None or target_pct is not None or trail_pct is not None:
            raise ValueError("time_exit is driven by expires_at alone — drop target_price/target_pct/trail_pct")
        if expires_at is None:
            raise ValueError("time_exit needs expires_at (ISO timestamp or datetime)")
        try:
            deadline = _to_utc(expires_at)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expires_at {expires_at!r} is not a valid ISO timestamp") from exc
        if deadline <= _now():
            raise ValueError(
                f"expires_at {deadline.isoformat(timespec='seconds')} is in the past — a time exit must point at "
                "a future moment"
            )
        expires_iso = deadline.isoformat(timespec="seconds")

    return ExitRule(
        id=uuid4().hex[:8],
        condition_id=position.condition_id,
        token_id=position.token_id,
        market_title=position.market_title,
        outcome=position.outcome,
        kind=kind,
        target_price=resolved_price,
        target_pct=resolved_pct,
        trail_pct=resolved_trail,
        exit_fraction=fraction,
        high_water_mark=hwm,
        expires_at=expires_iso,
        active=True,
        created_at=_now_iso(),
        note=note,
    )
