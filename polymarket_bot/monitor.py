"""The polling loop that makes stop-losses possible at all.

Polymarket has **no native stop order**. A take-profit can rest on the book as
a limit SELL and works while the bot is offline; a stop-loss, a trailing stop
and a time exit exist only in `rules.py` plus this file. If this loop is not
running, those rules protect nothing. Nothing here should ever be described as
a guarantee: a stop is an instruction to *try* to sell when a price is
observed, and the fill happens at whatever the book offers.

Design rules this module is built around:

  * **Live state, every pass.** Positions are re-read from the API on every
    sweep, never cached between passes. This is not theoretical - on this very
    account the owner sold a position on the website two minutes after the bot
    bought it.
  * **Absence is evidence, not proof.** A rule whose position is missing is
    NOT retired on the spot. `/positions` can answer empty or partial for one
    sweep while every holding is still there, and reading that as "sold" once
    disarmed every rule on the account in a single pass with no error
    recorded. Absence has to be confirmed on
    `_ABSENT_SWEEPS_BEFORE_RETIRE` consecutive sweeps, every unconfirmed
    sighting is reported as a failed read, and the retirement reason says out
    loud that a repeated API failure looks identical from here.
  * **A partial exit is not a finished exit.** Exits are FAK: the book fills
    what it can and kills the rest. A stop-loss on 100 shares that fills 2 has
    protected nothing, so the rule stays armed until the exit is
    substantially complete (see `_EXIT_COMPLETE_FRACTION`) and the report says
    how many shares are still exposed.
  * **Persist the ratchet before deciding, and only on a price worth
    ratcheting on.** A trailing stop's high-water mark is written to the rule
    store *before* the rule is evaluated, so a crash between two sweeps cannot
    roll the stop back down. Because it only ever moves up and is durable, it
    moves only on a fresh two-sided midpoint - never on a one-sided book or a
    stale data-api price, either of which would permanently raise the stop on
    one bad tick.
  * **Dry run is the default** (`settings.monitor_dry_run`). Triggers are
    reported and announced; nothing is sent.
  * **A rolling 24h realized-loss budget** (`settings.daily_loss_limit_usdc`)
    lives in `settings.state_path`. Once it is spent the monitor keeps
    watching and reporting but stops executing, and says why. A ledger that
    cannot be *read* halts executions too - it is not "$0 of losses today".
  * **One bad market never aborts the sweep.** Every rule and every API call is
    wrapped; failures land in `report.errors` and the loop continues.
  * **Repeats are announced on transition.** A halt, and a blocked exit, are
    alerted when they start and when they change, not once per interval
    forever. That state lives on the Monitor instance, which is why
    `run_forever` is the loop to run: rebuilding a Monitor every pass resets
    it.

The loss budget only counts exits *this monitor executed*: a loss taken by
hand on the website is invisible to it. Say that out loud rather than implying
the budget covers everything.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, NamedTuple
from uuid import uuid4

from polymarket import Market, SecureClient

from polymarket_bot import portfolio, trading
from polymarket_bot.config import Settings
from polymarket_bot.markets import get_book_snapshot, get_market_by_condition_id
from polymarket_bot.notify import Level, Notifier
from polymarket_bot.portfolio import PositionView
from polymarket_bot.rules import ExitRule, RuleStore, evaluate_rule

# Order size is quantized to 2 decimals for every tick size Polymarket
# supports, so anything finer can never become an order (mirrors
# trading._SIZE_QUANTUM deliberately rather than importing a private name).
_SIZE_QUANTUM = Decimal("0.01")

_STATE_VERSION = 1

# Realized losses are budgeted over a rolling 24h window; the file keeps a week
# so the ledger is still readable after a halt.
_LOSS_WINDOW_HOURS = 24
_STATE_RETENTION_HOURS = 24 * 7

# run_forever backoff: interval * 2^n, capped, reset on the first clean sweep.
_MAX_BACKOFF_DOUBLINGS = 5
_MAX_BACKOFF_SECONDS = 900.0

# How many list entries to_text() prints before summarising the rest.
_TEXT_LIMIT = 5

# --- when an exit counts as finished --------------------------------------
# Exits are sent FAK (fill-and-kill): the book matches what it can right now
# and cancels the remainder. So "a fill happened" and "the position is out" are
# different facts, and only the second one may retire a rule.
#
# A rule is retired when at least `_EXIT_COMPLETE_FRACTION` of the size that
# was actually sent came back filled. 98% is chosen to absorb exchange-side
# rounding on the 0.01-share grid without absorbing anything a human would
# notice: on a 100-share exit it tolerates 2 shares (a few cents), and on a
# 5-share exit the absolute floor below takes over instead. Anything larger
# than that remains held, remains exposed, and keeps its rule armed.
_EXIT_COMPLETE_FRACTION = 0.98
# ...and a remainder under one size-grid step can never be sold at all, so it
# always counts as complete however small the order was.
_EXIT_DUST_SHARES = 0.01

# --- how many empty reads retire a rule ------------------------------------
# A position missing from one `/positions` response is not proof it was sold.
# Three consecutive sweeps is ~3 minutes at the default 60s interval: long
# enough that a transient API blip cannot disarm the account, short enough that
# a genuinely sold position stops being polled quickly.
_ABSENT_SWEEPS_BEFORE_RETIRE = 3

# --- what a trailing ratchet may move on -----------------------------------
# The high-water mark is durable and only ever goes up, so a single bad read
# raises the stop permanently. A midpoint across a 0.01/0.99 book is arithmetic,
# not a traded price; refuse to ratchet on a book wider than this. Not
# ratcheting is always the safe direction - the stop simply stays where it is.
_RATCHET_MAX_SPREAD = 0.10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _floor_size(shares: float) -> float:
    """Floor to the exchange's 0.01-share grid. Never rounds up - a sell that
    rounds up is rejected for insufficient balance."""
    if shares <= 0:
        return 0.0
    return float(Decimal(str(shares)).quantize(_SIZE_QUANTUM, rounding=ROUND_DOWN))


def _outcome_for_token(market: Market, token_id: str) -> str:
    """Map a token back onto "yes"/"no" for build_sell_plan.

    Resolved from the market's own token ids rather than the rule's stored
    label: labels are free text ("Up"/"Down"/"Yes"), token ids are not.
    """
    if market.outcomes.yes.token_id == token_id:
        return "yes"
    if market.outcomes.no.token_id == token_id:
        return "no"
    raise ValueError(f"Token does not belong to market {market.condition_id}")


def _bullets(lines: list[str], limit: int = _TEXT_LIMIT) -> list[str]:
    out = [f"    - {line}" for line in lines[:limit]]
    if len(lines) > limit:
        out.append(f"    ... and {len(lines) - limit} more")
    return out


# --------------------------------------------------------------------------
# monitor state (the realized-loss ledger)
# --------------------------------------------------------------------------


def _empty_state() -> dict:
    return {"version": _STATE_VERSION, "realized_exits": []}


def _load_state(settings: Settings) -> dict:
    """Read the realized-loss ledger. **Raises OSError if it cannot be read.**

    The distinction this function draws is the whole daily-loss budget:

      * **Missing file** -> empty state. Nothing has been recorded yet, so
        "$0 of losses so far" is the truth.
      * **Damaged content** (not UTF-8, not JSON, not an object) -> empty
        state. The bytes are unrecoverable; the only alternative is a monitor
        that stays halted forever until a human deletes a file, which is worse
        than restarting a budget nobody can read anyway.
      * **Any other OSError** (a Windows sharing violation from a backup agent,
        a permissions problem, a disconnected drive) -> **raises**. The file is
        intact and the failure is transient, so swallowing it would report
        "$0 of losses today" for a ledger that may hold the exact losses that
        halted trading an hour ago - and `Monitor._halt_reason` would resume
        automatic selling on the strength of it. `loss_window_usdc` therefore
        propagates, and `_halt_reason` turns that into a halt.

    Same rule on the write side: `_record_realized` reads through here before
    appending, so an unreadable-but-intact ledger is never overwritten with a
    fresh one that has forgotten today's losses.
    """
    path = settings.state_path
    if not path.exists():
        return _empty_state()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Raced with a delete between the check and the read; an absent ledger
        # is an empty ledger, not a failure.
        return _empty_state()
    except ValueError:
        # UnicodeDecodeError: the bytes are damaged, not the access blocked.
        return _empty_state()
    # NOTE: OSError is deliberately NOT caught here. See the docstring.

    try:
        raw = json.loads(text)
    except ValueError:
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    raw.setdefault("realized_exits", [])
    if not isinstance(raw["realized_exits"], list):
        raw["realized_exits"] = []
    return raw


def _save_state(settings: Settings, state: dict) -> None:
    """Atomic write: temp file in the same directory, then os.replace, so a
    crash mid-write cannot leave a half-written loss ledger behind.

    Every failure raises, deliberately - a loss that silently failed to reach
    the ledger is a loss the budget will never see. The caller records the
    failure as an error saying the budget is now under-counted.
    """
    settings.ensure_data_dir()
    path = settings.state_path
    state["version"] = _STATE_VERSION
    state["updated_at"] = _now_iso()
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid4().hex[:6]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _entry_time(entry: dict) -> datetime | None:
    raw = entry.get("at")
    if not raw:
        return None
    try:
        text = str(raw)
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class _PriceRead(NamedTuple):
    """One token's observed price for this sweep, plus how far to trust it.

    `trusted` means specifically: good enough to move a trailing stop's
    high-water mark up on. That is a higher bar than "good enough to evaluate a
    rule against", because the HWM is persisted and only ever travels one way -
    a bad read raises the stop for good, while a bad read used only for an
    evaluation is forgotten on the next sweep.

    So a fresh midpoint from a two-sided book with a sane spread is trusted; a
    one-sided book (best bid or best ask alone) and the data-api's last known
    price are not. Both are still used to *evaluate* rules - refusing to
    evaluate would silently disable a stop-loss exactly when the book is thin,
    which is when the owner most wants out.
    """

    price: float | None
    source: str
    error: str | None
    trusted: bool


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


@dataclass
class MonitorReport:
    """What one sweep saw and did. JSON-safe throughout.

    `triggered` is what fired; `executed` is what was actually sent (empty in
    dry run, and empty once halted); `deactivated` is rules retired this pass -
    position sold elsewhere, market resolved, holding now dust, or exit filled.
    """

    checked_at: str
    positions_checked: int = 0
    rules_evaluated: int = 0
    triggered: list[dict] = field(default_factory=list)
    executed: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True
    halted_reason: str | None = None
    # Beyond the contract's field list, but the contract requires retired rules
    # to be reported somewhere and they are not errors.
    deactivated: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "positions_checked": self.positions_checked,
            "rules_evaluated": self.rules_evaluated,
            "triggered": [dict(t) for t in self.triggered],
            "executed": [dict(e) for e in self.executed],
            "errors": list(self.errors),
            "dry_run": self.dry_run,
            "halted_reason": self.halted_reason,
            "deactivated": [dict(d) for d in self.deactivated],
        }

    def to_text(self) -> str:
        """Compact ASCII status block - fits a Telegram message and survives a
        non-UTF-8 Windows console."""
        mode = (
            "DRY RUN (evaluating only, nothing is sent)"
            if self.dry_run
            else "LIVE (triggers are executed)"
        )
        lines = [
            f"MONITOR SWEEP - {self.checked_at}",
            f"  Mode      : {mode}",
            f"  Scanned   : {self.positions_checked} position(s), "
            f"{self.rules_evaluated} rule(s) evaluated",
        ]

        if self.halted_reason:
            lines.append(f"  HALTED    : {self.halted_reason}")

        if self.deactivated:
            lines.append(f"  Retired   : {len(self.deactivated)} rule(s)")
            lines.extend(
                _bullets(
                    [
                        f"{d.get('kind', '?')} '{d.get('outcome', '')}' "
                        f"{d.get('market_title', '')}: {d.get('reason', '')}"
                        for d in self.deactivated
                    ]
                )
            )

        if self.triggered:
            lines.append(f"  Triggered : {len(self.triggered)}")
            for item in self.triggered[:_TEXT_LIMIT]:
                price = item.get("price")
                price_text = f"{price:.4f}" if isinstance(price, (int, float)) else "?"
                shares = item.get("exit_shares") or 0.0
                lines.append(
                    f"    - {item.get('kind', '?')} @ {price_text} -> sell {shares:,.2f} sh "
                    f"| {item.get('market_title', '')} '{item.get('outcome', '')}' "
                    f"[{str(item.get('action', '')).upper()}]"
                )
                reason = item.get("reason")
                if reason:
                    lines.append(f"      {reason}")
            if len(self.triggered) > _TEXT_LIMIT:
                lines.append(f"    ... and {len(self.triggered) - _TEXT_LIMIT} more")
        else:
            lines.append("  Triggered : none")

        if self.executed:
            lines.append(f"  Executed  : {len(self.executed)}")
            rows = []
            for item in self.executed:
                if item.get("ok"):
                    filled = item.get("filled_shares") or 0.0
                    usdc = item.get("filled_usdc") or 0.0
                    pnl = item.get("realized_pnl")
                    pnl_text = f" | P&L {pnl:+.2f}" if isinstance(pnl, (int, float)) else ""
                    # A partial exit must never read as a finished one: the
                    # remainder is still held and still exposed.
                    tail = ""
                    if item.get("partial"):
                        left = item.get("shares_remaining") or 0.0
                        tail = f" | PARTIAL - {left:,.2f} sh unsold, rule STILL ARMED"
                    rows.append(
                        f"{'PART' if item.get('partial') else 'OK  '} SELL {filled:,.2f} sh "
                        f"for ${usdc:.2f}{pnl_text}{tail} | {item.get('market_title', '')}"
                    )
                else:
                    # Blocked means nothing was sent and the rule is still
                    # armed; failed means the exchange saw it and said no.
                    tag = "BLOCKED" if item.get("blockers") else "FAILED "
                    rows.append(
                        f"{tag} {item.get('market_title', '')}: {item.get('error') or 'unknown error'}"
                    )
            lines.extend(_bullets(rows))

        if self.errors:
            lines.append(f"  Errors    : {len(self.errors)}")
            lines.extend(_bullets(self.errors))

        return "\n".join(lines)


# --------------------------------------------------------------------------
# monitor
# --------------------------------------------------------------------------


class Monitor:
    """Polls live prices and enforces stored exit rules.

    Read-only unless `settings.monitor_dry_run` is False, and even then the
    only write path is `trading.execute_plan` (which re-verifies against live
    state and refuses on any blocker).
    """

    def __init__(
        self,
        client: SecureClient,
        settings: Settings,
        *,
        notifier: Notifier | None = None,
        store: RuleStore | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.notifier = notifier
        self.store = store if store is not None else RuleStore(settings)
        # Set when a sweep could not read live state at all; run_forever uses it
        # to back off instead of hammering a down API every interval.
        self._last_run_failed = False
        # Halt/resume is announced on transition only - otherwise a halted
        # monitor would alert every single interval.
        self._halt_announced = False
        # rule_id -> consecutive sweeps in which its position was missing. A
        # rule is only retired once this reaches _ABSENT_SWEEPS_BEFORE_RETIRE,
        # so one empty /positions response cannot disarm the account.
        self._absent_sweeps: dict[str, int] = {}
        # rule_id -> the blocker text last announced for it, so an exit that is
        # blocked for an hour produces one alert rather than sixty.
        self._blocked_reasons: dict[str, str] = {}
        # Rules retired during this process, whether or not the store write
        # succeeded. Consulted before every evaluation so a failed persist can
        # never resurrect a rule mid-run and sell the same position twice.
        self._retired: set[str] = set()
        # ALL of the above is per-instance. A caller that builds a new Monitor
        # every pass resets it and gets an alert storm plus rules that can never
        # accumulate a confirmed absence - use run_forever, not a loop around
        # a fresh Monitor.

    # ---- plumbing ----------------------------------------------------

    def _notify(self, message: str, level: Level = "info") -> None:
        # A broken sink must never break the sweep.
        if self.notifier is None:
            return
        try:
            self.notifier.send(message, level=level)
        except Exception:
            pass

    # ---- loss budget -------------------------------------------------

    def loss_window_usdc(self) -> float:
        """Realized losses this monitor booked in the last 24h, as a positive
        number. Gains do not refill the budget - the limit is a loss budget,
        not a net-P&L budget, so a winning trade cannot buy back the right to
        keep losing.

        Raises whatever `_load_state` raises (an OSError on a ledger that is
        present but unreadable). It must: a budget that answers "$0" when it
        cannot see its own records is not a budget.
        """
        cutoff = _now() - timedelta(hours=_LOSS_WINDOW_HOURS)
        total = 0.0
        for entry in _load_state(self.settings).get("realized_exits", []):
            if not isinstance(entry, dict):
                continue
            moment = _entry_time(entry)
            if moment is None or moment < cutoff:
                continue
            try:
                total += max(float(entry.get("loss") or 0.0), 0.0)
            except (TypeError, ValueError):
                continue
        return round(total, 6)

    def _halt_reason(self) -> str | None:
        """None when executions may proceed, otherwise why they may not.

        A non-positive limit disables the check: "halt once losses reach $0"
        would mean permanently halted, which is not a usable setting.
        """
        limit = float(self.settings.daily_loss_limit_usdc)
        if limit <= 0:
            return None
        try:
            lost = self.loss_window_usdc()
        except Exception as exc:
            # FAIL CLOSED. An unreadable ledger is indistinguishable from a
            # ledger full of losses, so it stops automatic selling rather than
            # reading as a fresh $0 budget. This branch is only reachable
            # because `_load_state` lets an OSError through - it used to
            # swallow one and return empty state, which made this dead code.
            return (
                f"Could not read the loss ledger at {self.settings.state_path} "
                f"({type(exc).__name__}: {exc}) - executions paused. The 24h loss budget cannot be "
                f"verified, so it is treated as spent; rules are still watched and reported."
            )
        if lost >= limit:
            return (
                f"Daily loss limit reached: ${lost:.2f} of realized losses in the last "
                f"{_LOSS_WINDOW_HOURS}h vs a ${limit:.2f} limit. Rules are still watched and "
                f"reported, but nothing will be sold automatically until the window clears."
            )
        return None

    def _record_realized(self, pnl: float, *, rule: ExitRule, filled_usdc: float, filled_shares: float) -> None:
        """Append one executed exit to the rolling ledger.

        P&L is `proceeds - filled_shares * average entry price` read live just
        before the sell. It ignores fees and any earlier partial exits, so it
        is an estimate for budgeting, not accounting truth.

        The read-modify-write goes through `_load_state`, which raises rather
        than returning empty state on an unreadable-but-intact file. That is
        the point: rewriting a ledger we could not read would drop the very
        losses the budget exists to count.
        """
        state = _load_state(self.settings)
        entries = [e for e in state.get("realized_exits", []) if isinstance(e, dict)]
        entries.append(
            {
                "at": _now_iso(),
                "rule_id": rule.id,
                "kind": rule.kind,
                "market_title": rule.market_title,
                "outcome": rule.outcome,
                "token_id": rule.token_id,
                "shares": round(float(filled_shares), 6),
                "proceeds_usdc": round(float(filled_usdc), 6),
                "pnl": round(float(pnl), 6),
                "loss": round(max(-float(pnl), 0.0), 6),
            }
        )
        cutoff = _now() - timedelta(hours=_STATE_RETENTION_HOURS)
        state["realized_exits"] = [e for e in entries if (_entry_time(e) or _now()) >= cutoff]
        _save_state(self.settings, state)

    # ---- pricing -----------------------------------------------------

    def _price_for(
        self,
        token_id: str,
        position: PositionView,
        cache: dict[str, _PriceRead],
    ) -> _PriceRead:
        """One token's price for this sweep, cached. See `_PriceRead`.

        Prefers the book midpoint over the best bid on purpose: the bid is what
        a sale would actually fetch, but on a one-sided book a single lowball
        bid would trip every stop-loss on the account. The real fill price is
        estimated again by `build_sell_plan` before anything is sent.
        """
        if token_id in cache:
            return cache[token_id]

        price: float | None = None
        source = "unavailable"
        error: str | None = None
        trusted = False
        try:
            snapshot = get_book_snapshot(self.client, token_id)
        except Exception as exc:
            snapshot = None
            error = f"order book unreadable ({type(exc).__name__}: {exc})"

        if snapshot is not None:
            spread = snapshot.spread
            if snapshot.midpoint is not None:
                price, source = snapshot.midpoint, "book midpoint"
                # Both sides resting AND a sane spread. A midpoint across a
                # 0.01/0.99 book is the average of two prices nobody will
                # trade at; ratcheting a durable one-way stop onto it is how a
                # single bad tick permanently raises the stop.
                trusted = (
                    0.0 < float(price) < 1.0
                    and spread is not None
                    and spread <= _RATCHET_MAX_SPREAD
                )
                if spread is not None and spread > _RATCHET_MAX_SPREAD:
                    source = f"book midpoint across a wide {spread:.4f} spread"
            elif snapshot.best_bid is not None:
                price, source = snapshot.best_bid, "best bid (no asks resting)"
            elif snapshot.best_ask is not None:
                price, source = snapshot.best_ask, "best ask (no bids resting)"
            else:
                error = "order book is empty on both sides"

        if price is None and 0.0 < position.cur_price <= 1.0:
            # Last resort: the data-api price on the position. It can lag the
            # book by minutes, so it is labelled as such wherever it is used.
            price, source = position.cur_price, "data-api last price (book unavailable)"

        result = _PriceRead(
            price=round(float(price), 6) if price is not None else None,
            source=source,
            error=error,
            trusted=trusted,
        )
        cache[token_id] = result
        return result

    # ---- rule lifecycle ----------------------------------------------

    def _deactivate(self, rule: ExitRule, report: MonitorReport, reason: str, *, level: Level = "alert") -> None:
        """Retire a rule and say so.

        The retirement is recorded in memory FIRST and unconditionally. If the
        store write then fails (another process holding the lock, a read-only
        disk), the rule is still dead for the lifetime of this process. It used
        to be reported as retired while staying `active=True` on disk, so the
        next sweep re-fired it and sent a SECOND sell - repeatedly slicing the
        position for a partial-exit rule. A failed write must degrade to "not
        persisted across restart", never to "fires again in 60 seconds".
        """
        self._retired.add(rule.id)
        entry = {
            "rule_id": rule.id,
            "kind": rule.kind,
            "market_title": rule.market_title,
            "outcome": rule.outcome,
            "token_id": rule.token_id,
            "condition_id": rule.condition_id,
            "reason": reason,
            "persisted": False,
        }
        try:
            self.store.update(replace(rule, active=False))
            entry["persisted"] = True
        except Exception as exc:
            report.errors.append(
                f"rule {rule.id}: deactivation could not be saved ({type(exc).__name__}: {exc}). "
                f"It is retired for this process and will NOT fire again, but the rules file "
                f"still shows it active - it would come back after a restart."
            )
        report.deactivated.append(entry)
        # Per-rule bookkeeping dies with the rule; leaving it would leak, and
        # would suppress the first alert if the rule were ever re-armed.
        self._absent_sweeps.pop(rule.id, None)
        self._blocked_reasons.pop(rule.id, None)
        self._notify(
            f"Rule {rule.id} retired ({rule.kind} on '{rule.outcome}' - {rule.market_title}): {reason}",
            level,
        )

    def _note_absent(self, rule: ExitRule, report: MonitorReport, *, positions_empty: bool) -> None:
        """A rule whose position is not in this sweep's positions read.

        Absence is evidence, not proof, and the difference is a disarmed
        account. A single empty or partial `/positions` response used to be
        treated as "the position was sold": every rule was deactivated and
        persisted in one pass, `report.errors` stayed empty, and the reason
        string asserted a sale that never happened. Stop-losses do not come
        back on their own.

        So an unconfirmed absence is a FAILED read: it is recorded as an error,
        surfaced, and leaves the rule ARMED. Only
        `_ABSENT_SWEEPS_BEFORE_RETIRE` consecutive absences retire it, and the
        retirement reason says plainly that a repeated API failure is
        indistinguishable from a sale when seen from here.

        An EMPTY positions response never counts toward that streak at all.
        `run_once` already records an empty read as a failed sweep, so counting
        it here as evidence of a sale would have the monitor contradict itself -
        and three minutes of data-api trouble would retire every rule on the
        account. Retirement requires a SUCCESSFUL, non-empty read that simply
        does not contain this token.
        """
        context = (
            "the whole positions response was empty"
            if positions_empty
            else "other positions came back, but not this one"
        )

        if positions_empty:
            message = (
                f"rule {rule.id} ({rule.market_title} '{rule.outcome}'): positions could not be "
                f"read ({context}) - this sweep proves nothing about the position, so the rule "
                f"stays ARMED and this does not count toward retirement."
            )
            report.errors.append(message)
            self._notify(message, "alert")
            return

        streak = self._absent_sweeps.get(rule.id, 0) + 1
        self._absent_sweeps[rule.id] = streak

        if streak < _ABSENT_SWEEPS_BEFORE_RETIRE:
            message = (
                f"rule {rule.id} ({rule.market_title} '{rule.outcome}'): position missing from the "
                f"live positions read ({context}) - absence {streak} of "
                f"{_ABSENT_SWEEPS_BEFORE_RETIRE}. The rule stays ARMED; one bad /positions "
                f"response is not proof the position was sold."
            )
            report.errors.append(message)
            self._notify(message, "alert")
            return

        self._deactivate(
            rule,
            report,
            f"Position was absent from {streak} consecutive live position reads "
            f"({context} on the last one). The likely explanation is that it was sold, merged or "
            f"transferred outside the bot - but {streak} failed reads look identical from here, so "
            f"if the position is in fact still open, re-arm this rule.",
        )

    def _ratchet(self, rule: ExitRule, price: float, report: MonitorReport) -> ExitRule:
        """Advance and PERSIST a trailing stop's high-water mark before it is
        evaluated. Persisting first is the whole point: a crash between sweeps
        must not let the stop slide back down to an older, lower high."""
        stored = rule.high_water_mark
        if stored is not None and price <= float(stored) + 1e-9:
            return rule
        updated = replace(rule, high_water_mark=round(price, 6))
        try:
            self.store.update(updated)
        except Exception as exc:
            # Evaluate on the new high anyway (evaluate_rule folds the current
            # price in regardless), but flag that the ratchet is not durable.
            report.errors.append(
                f"rule {rule.id}: new high-water mark {price:.4f} could not be saved "
                f"({type(exc).__name__}: {exc}); the ratchet will reset if the bot restarts."
            )
        return updated

    # ---- one sweep ---------------------------------------------------

    def run_once(self) -> MonitorReport:
        """One pass: re-read positions, ratchet trailing stops, evaluate every
        active rule, and (unless dry-run or halted) execute the exits that
        fired. Never raises for expected failures - they land in
        `report.errors` so a single bad market cannot stop the sweep."""
        report = MonitorReport(
            checked_at=_now_iso(),
            dry_run=bool(self.settings.monitor_dry_run),
        )
        self._last_run_failed = False

        # Live positions, always. Cached position state is how you end up
        # trying to sell something the owner already sold on the website.
        try:
            positions = portfolio.get_positions(self.client, include_resolved=True)
        except Exception as exc:
            message = (
                f"Could not read live positions ({type(exc).__name__}: {exc}) - "
                f"no rule was evaluated this pass."
            )
            report.errors.append(message)
            self._notify(message, "error")
            self._last_run_failed = True
            return report

        report.positions_checked = len(positions)
        by_token: dict[str, PositionView] = {}
        for view in positions:
            if view.token_id:
                by_token.setdefault(view.token_id, view)

        try:
            rules = self.store.list(active_only=True)
        except Exception as exc:
            message = f"Could not read the rule store ({type(exc).__name__}: {exc})."
            report.errors.append(message)
            self._notify(message, "error")
            self._last_run_failed = True
            return report

        # Checked before the "no rules" exit so a status call always reports a
        # halt, whether or not anything is currently being watched.
        report.halted_reason = self._halt_reason()
        if report.halted_reason and not self._halt_announced:
            self._notify(report.halted_reason, "alert")
            self._halt_announced = True
        elif not report.halted_reason and self._halt_announced:
            self._notify("Daily loss budget has room again - automatic exits are live.", "alert")
            self._halt_announced = False

        if not rules:
            return report

        # An empty positions list while rules are armed is a FAILED READ, not a
        # discovery that everything was sold. The data-api has answered empty
        # for one sweep with every holding intact; acting on that once
        # deactivated and persisted every rule on the account with
        # report.errors == []. (portfolio.py now asks for size_threshold=0,
        # which removes one cause of a wrongly-empty list - it does not remove
        # the transient-response cause, which is what this guards.)
        positions_empty = not positions
        if positions_empty:
            message = (
                f"Live positions came back EMPTY while {len(rules)} rule(s) are armed. Treating "
                f"that as a failed read, not as proof the positions were sold - no rule is retired "
                f"on a single empty response."
            )
            report.errors.append(message)
            self._notify(message, "error")
            self._last_run_failed = True

        price_cache: dict[str, _PriceRead] = {}
        for rule in rules:
            try:
                self._process_rule(rule, by_token, price_cache, report, positions_empty=positions_empty)
            except Exception as exc:
                # Belt and braces: _process_rule already handles its own
                # failures, but one unexpected error must not end the sweep.
                message = f"rule {rule.id} ({rule.market_title}): {type(exc).__name__}: {exc}"
                report.errors.append(message)
                self._notify(f"Rule check failed - {message}", "error")

        return report

    def _process_rule(
        self,
        rule: ExitRule,
        by_token: dict[str, PositionView],
        price_cache: dict[str, _PriceRead],
        report: MonitorReport,
        *,
        positions_empty: bool = False,
    ) -> None:
        # Retired earlier in this process. The store may still say active=True
        # if that write failed; acting on it would re-sell a position we have
        # already exited, so memory wins.
        if rule.id in self._retired:
            return

        position = by_token.get(rule.token_id)

        if position is None or position.shares <= 0:
            self._note_absent(rule, report, positions_empty=positions_empty)
            return

        # A confirmed sighting resets the streak: absences only retire a rule
        # when they are CONSECUTIVE.
        self._absent_sweeps.pop(rule.id, None)

        if position.is_resolved:
            self._deactivate(
                rule,
                report,
                "Market has resolved. A settled position is redeemed, not sold, so this rule "
                "can never fire - redeem the position instead.",
            )
            return

        sellable = _floor_size(position.shares * min(float(rule.exit_fraction), 1.0))
        if sellable <= 0:
            self._deactivate(
                rule,
                report,
                f"Holding is dust: {rule.exit_fraction:g} of {position.shares:,.4f} shares rounds to 0 "
                f"on the exchange's 0.01-share grid, so no order could ever be built.",
            )
            return

        read = self._price_for(rule.token_id, position, price_cache)
        price, source = read.price, read.source
        if price is None:
            if rule.kind != "time_exit":
                report.errors.append(
                    f"rule {rule.id} ({rule.market_title}): no usable price "
                    f"({read.error or 'no source available'}) - skipped this pass."
                )
                return
            # A time exit is decided by the clock alone - `evaluate_rule` never
            # looks at the price for it. Gating every rule on a readable book
            # meant a due time exit was skipped forever whenever the book was
            # down, which is precisely when an unreadable market is worth
            # leaving.
            report.errors.append(
                f"rule {rule.id} ({rule.market_title}): no usable price "
                f"({read.error or 'no source available'}); the time exit is evaluated anyway - "
                f"it is decided by the clock, not the book."
            )
        elif read.error:
            report.errors.append(f"rule {rule.id} ({rule.market_title}): {read.error}; using {source}.")

        ratcheted = False
        if rule.kind == "trailing_stop" and price is not None:
            if read.trusted:
                before = rule.high_water_mark
                rule = self._ratchet(rule, price, report)
                ratcheted = rule.high_water_mark != before
            else:
                # The ratchet is durable and one-way, so it moves only on a
                # price worth trusting. A one-sided book or a stale data-api
                # quote is still evaluated against below - it just cannot raise
                # the stop permanently on one bad tick.
                mark = (
                    f"{rule.high_water_mark:.4f}" if rule.high_water_mark is not None else "unset"
                )
                self._notify(
                    f"[{rule.id}] high-water mark left at {mark}: {price:.4f} came from {source}, "
                    f"which is not a read a one-way ratchet should move on.",
                    "debug",
                )

        # NaN rather than None keeps the annotated float signature honest;
        # evaluate_rule treats any out-of-range price as "no observation", and
        # only time_exit ever reaches here without one.
        decision = evaluate_rule(rule, position, price if price is not None else float("nan"))
        report.rules_evaluated += 1

        if not decision.should_exit:
            self._notify(f"[{rule.id}] {rule.market_title}: {decision.reason}", "debug")
            return

        entry: dict[str, Any] = {
            "rule_id": rule.id,
            "kind": rule.kind,
            "condition_id": rule.condition_id,
            "token_id": rule.token_id,
            "market_title": rule.market_title,
            "outcome": rule.outcome,
            "price": price,
            "price_source": source,
            "price_trusted": read.trusted,
            "hwm_ratcheted": ratcheted,
            "exit_shares": decision.exit_shares,
            "shares_held": position.shares,
            "avg_price": position.avg_price,
            "exit_fraction": rule.exit_fraction,
            "high_water_mark": rule.high_water_mark,
            "unrealized_pnl": position.unrealized_pnl,
            "reason": decision.reason,
            "action": "pending",
        }
        report.triggered.append(entry)
        self._notify(
            f"TRIGGER {rule.kind} [{rule.id}] {rule.market_title} '{rule.outcome}': {decision.reason} "
            f"(price from {source}; exit {decision.exit_shares:,.2f} of {position.shares:,.2f} shares)",
            "alert",
        )

        if report.dry_run:
            # Dry run wins over every other gate: nothing is built, nothing is
            # sent. A halt in force is still noted so the report says both.
            entry["action"] = "dry_run"
            entry["note"] = "DRY RUN - no order was built or sent (POLYMARKET_MONITOR_DRY_RUN)."
            if report.halted_reason:
                entry["note"] += f" Also halted: {report.halted_reason}"
            self._notify(f"[{rule.id}] DRY RUN - nothing sent.", "alert")
            return

        if report.halted_reason:
            entry["action"] = "halted"
            entry["note"] = report.halted_reason
            self._notify(f"[{rule.id}] Exit NOT executed - {report.halted_reason}", "alert")
            return

        self._execute(rule, position, decision.exit_shares, entry, report)

    # ---- execution ---------------------------------------------------

    def _execute(
        self,
        rule: ExitRule,
        position: PositionView,
        exit_shares: float,
        entry: dict,
        report: MonitorReport,
    ) -> None:
        """Build and send the exit. WRITE PATH - only reached when dry-run is
        off and the loss budget still has room."""
        record: dict[str, Any] = {
            "rule_id": rule.id,
            "kind": rule.kind,
            "condition_id": rule.condition_id,
            "token_id": rule.token_id,
            "market_title": rule.market_title,
            "outcome": rule.outcome,
            "shares_requested": exit_shares,
            "ok": False,
            "status": None,
            "order_id": None,
            "filled_shares": 0.0,
            "filled_usdc": 0.0,
            "avg_price": None,
            "realized_pnl": None,
            "tx_hashes": [],
            "blockers": [],
            "error": None,
            # Always present so a reader never has to infer them from absence.
            "partial": False,
            "shares_remaining": None,
            "rule_deactivated": False,
        }

        try:
            market = get_market_by_condition_id(self.client, rule.condition_id)
            outcome_key = _outcome_for_token(market, rule.token_id)
            plan = trading.build_sell_plan(
                self.client,
                self.settings,
                market=market,
                outcome=outcome_key,
                shares=exit_shares,
            )
        except Exception as exc:
            record["error"] = f"Could not build the exit order ({type(exc).__name__}: {exc})."
            entry["action"] = "failed"
            report.executed.append(record)
            report.errors.append(f"rule {rule.id}: {record['error']}")
            self._notify(f"[{rule.id}] {record['error']}", "error")
            return

        if not plan.is_executable():
            # Rule stays active on purpose: a blocker (market halted, nothing
            # on the book to price against) may clear, and silently retiring a
            # stop-loss because one sweep could not build the order would
            # remove the protection it exists for.
            #
            # It used to alert every single sweep while it stayed blocked. With
            # the old proceeds cap on sells that was permanent - a position
            # worth more than max_order_usdc could never be exited, so the
            # monitor emitted the same alert every 60s forever. trading.py no
            # longer caps sells, so the deadlock is gone; the de-duplication
            # below is what stops any *remaining* blocker from doing the same.
            # Announce on transition, like the halt logic: first time, and
            # again whenever the reason changes.
            record["blockers"] = list(plan.blockers)
            record["error"] = "; ".join(plan.blockers)
            entry["action"] = "blocked"
            entry["note"] = record["error"]
            report.executed.append(record)

            previous = self._blocked_reasons.get(rule.id)
            level: Level = "debug"
            prefix = "Exit still BLOCKED (unchanged, not re-alerting)"
            if previous is None:
                level, prefix = "alert", "Exit BLOCKED"
            elif previous != record["error"]:
                level, prefix = "alert", "Exit BLOCKED (reason changed)"
            self._blocked_reasons[rule.id] = str(record["error"])
            self._notify(
                f"[{rule.id}] {prefix} for '{rule.market_title}': {record['error']} "
                f"(rule stays active and will retry next sweep)",
                level,
            )
            return

        if self._blocked_reasons.pop(rule.id, None) is not None:
            # Transition the other way, so a block that clears is as visible as
            # one that starts.
            self._notify(
                f"[{rule.id}] Exit is no longer blocked for '{rule.market_title}' - sending it now.",
                "alert",
            )

        # execute_plan re-verifies against live state, refuses on any blocker,
        # and turns SDK failures into ok=False rather than exceptions.
        result = trading.execute_plan(self.client, self.settings, plan, notifier=self.notifier)

        record["ok"] = bool(result.ok)
        record["status"] = result.status
        record["order_id"] = result.order_id
        record["filled_shares"] = result.filled_shares
        record["filled_usdc"] = result.filled_usdc
        record["avg_price"] = result.avg_price
        record["tx_hashes"] = list(result.tx_hashes)
        record["error"] = result.error
        entry["action"] = "executed" if result.ok else "failed"
        report.executed.append(record)

        if not result.ok:
            report.errors.append(f"rule {rule.id}: exit failed - {result.error}")
            return

        if result.filled_shares <= 0:
            # Accepted but nothing matched. Leave the rule active so the next
            # sweep tries again against a fresh book.
            entry["action"] = "unfilled"
            entry["note"] = "Order accepted but nothing filled; rule stays active."
            self._notify(
                f"[{rule.id}] Exit accepted but unfilled ({rule.market_title}) - will retry next sweep.",
                "alert",
            )
            return

        # Estimated realized P&L on the shares just sold: proceeds minus what
        # they cost at the position's average entry. Fees are not modelled.
        pnl = round(result.filled_usdc - result.filled_shares * position.avg_price, 6)
        record["realized_pnl"] = pnl
        record["realized_pnl_basis"] = (
            "proceeds minus filled shares at the position's average entry price; excludes fees"
        )
        try:
            self._record_realized(pnl, rule=rule, filled_usdc=result.filled_usdc, filled_shares=result.filled_shares)
        except Exception as exc:
            report.errors.append(
                f"rule {rule.id}: exit filled but the loss ledger could not be updated "
                f"({type(exc).__name__}: {exc}) - the daily loss budget is now under-counted."
            )

        # --- did the exit actually happen? ---------------------------------
        # Exits are FAK: the book matches what it can and kills the rest, so a
        # non-zero fill is NOT the same fact as "the position is out". Retiring
        # the rule on any fill is how a stop-loss on 100 shares that filled 2
        # ends up reported as "Exit filled: 2.00 shares" while 98 shares sit
        # unprotected with nothing left watching them.
        #
        # Measured against what was SENT (`plan.shares`), not against the size
        # the rule asked for: build_sell_plan floors to the 0.01-share grid and
        # clamps to the live holding, so a fully-filled smaller order is a
        # complete exit, not a 99% one.
        requested = float(plan.shares) if plan.shares else float(exit_shares)
        remaining = round(max(requested - result.filled_shares, 0.0), 6)
        tolerance = max(_EXIT_DUST_SHARES, requested * (1.0 - _EXIT_COMPLETE_FRACTION))
        record["shares_remaining"] = remaining
        record["completeness_tolerance"] = round(tolerance, 6)

        if remaining > tolerance + 1e-9:
            still_held = round(max(position.shares - result.filled_shares, 0.0), 6)
            record["partial"] = True
            record["rule_deactivated"] = False
            entry["action"] = "partial"
            entry["note"] = (
                f"Partial exit: {result.filled_shares:,.2f} of {requested:,.2f} shares filled, "
                f"{remaining:,.2f} still to sell. Rule stays ACTIVE."
            )
            self._notify(
                f"[{rule.id}] Exit PARTIALLY filled ({rule.kind} on '{rule.outcome}' - "
                f"{rule.market_title}): sold {result.filled_shares:,.2f} of {requested:,.2f} shares "
                f"for ${result.filled_usdc:.2f}. About {still_held:,.2f} shares remain held; the "
                f"rule stays ARMED and will try the remainder next sweep.",
                "alert",
            )
        else:
            # Substantially complete. One rule fires once - a finished rule
            # left active would keep re-slicing whatever is left over.
            self._deactivate(
                rule,
                report,
                f"Exit filled: {result.filled_shares:,.2f} of {requested:,.2f} shares for "
                f"${result.filled_usdc:.2f} (estimated P&L {pnl:+.2f}).",
                level="trade",
            )
            record["rule_deactivated"] = True

        # Losses can push us over the budget mid-sweep; stop the rest of the pass.
        reason = self._halt_reason()
        if reason:
            report.halted_reason = reason
            if not self._halt_announced:
                self._notify(reason, "alert")
                self._halt_announced = True

    # ---- loop --------------------------------------------------------

    def _emit_report(self, iteration: int, report: MonitorReport) -> None:
        """Push a whole sweep out through the notifier.

        The loop used to emit a one-line count at "debug", which every default
        sink drops - so a monitor watching real money for hours said nothing at
        all and could not be told apart from a dead process. The full report
        text goes out on every sweep now; only the LEVEL varies, so a quiet
        sweep is still filtered out by a console at its default threshold while
        anything that actually happened is not.
        """
        if report.errors:
            level: Level = "error"
        elif report.triggered or report.executed or report.deactivated:
            level = "alert"
        else:
            level = "debug"
        self._notify(f"Sweep {iteration}\n{report.to_text()}", level)

    def run_forever(self, *, max_iterations: int | None = None) -> None:
        """Sweep every `settings.monitor_interval_seconds` until interrupted.

        **This is the loop to run.** Not a caller-side `while True` around
        `service.monitor_once()`: that builds a fresh Monitor (and a fresh
        client auth handshake) every pass, which resets `_halt_announced`,
        `_blocked_reasons` and `_absent_sweeps`. The documented
        announce-on-transition behaviour then never happens - a halted monitor
        alerts on every single interval, a blocked exit does the same, and a
        rule can never accumulate the consecutive absences it needs before
        being retired. One client, one Monitor, one loop, all the way through.

        Consecutive failed sweeps back off exponentially (capped) so an API
        outage is not hammered; the first clean sweep resets the delay. A sweep
        counts as failed only when live state could not be read at all
        (`_last_run_failed`) - a sweep that merely collected per-rule errors
        keeps the normal cadence, because those rules still need watching.

        Ctrl-C stops cleanly and says what stops being enforced.
        `max_iterations` exists for tests - it never sleeps after the final
        pass.
        """
        base = max(float(self.settings.monitor_interval_seconds), 1.0)
        mode = "DRY RUN" if self.settings.monitor_dry_run else "LIVE"
        self._notify(
            f"Monitor started ({mode}) - checking every {base:g}s. "
            f"Stop-losses only exist while this is running.",
            "info",
        )

        iteration = 0
        failures = 0
        try:
            while True:
                iteration += 1
                try:
                    report = self.run_once()
                except Exception as exc:  # run_once is defensive, but never die here
                    # KeyboardInterrupt is a BaseException and is deliberately
                    # NOT caught here - Ctrl-C must reach the handler below
                    # rather than being counted as a failed sweep.
                    self._last_run_failed = True
                    failures += 1
                    self._notify(
                        f"Sweep {iteration} crashed ({type(exc).__name__}: {exc}) - no rule was "
                        f"evaluated this pass.",
                        "error",
                    )
                else:
                    failures = failures + 1 if self._last_run_failed else 0
                    self._emit_report(iteration, report)

                if max_iterations is not None and iteration >= max_iterations:
                    break

                delay = min(base * (2 ** min(failures, _MAX_BACKOFF_DOUBLINGS)), _MAX_BACKOFF_SECONDS)
                if failures:
                    self._notify(
                        f"{failures} failed sweep(s) in a row - next check in {delay:g}s "
                        f"instead of {base:g}s.",
                        "error",
                    )
                time.sleep(delay)
        except KeyboardInterrupt:
            self._notify(
                f"Monitor stopped (Ctrl-C) after {iteration} sweep(s). Stop-loss, trailing-stop "
                f"and time-exit rules are NOT enforced while it is off.",
                "alert",
            )
            return

        self._notify(f"Monitor finished after {iteration} sweep(s).", "info")
