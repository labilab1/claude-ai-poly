"""Order construction and execution - the only module here that can move money.

Deliberately two-step. `build_buy_plan` / `build_sell_plan` are **read-only**:
they price the order, check it against the account and the risk limits, and
return an `OrderPlan` you can read in a terminal or paste into a chat.
`execute_plan` is the single function that actually posts an order.

Every plan sorts its objections into two piles:

  * **blockers** - `execute_plan` refuses, no confirmation flag overrides them.
    Over the per-order cap (BUYs), market not accepting orders, shares you do
    not own, cash you do not have, over the per-market position cap, a limit
    price the exchange would reject, a sell nothing can price, a market order
    with no protective price, and a market order whose own slippage guard
    forbids its own estimated fill (it could only ever match dust).
  * **warnings** - informational. The order can still be sent; you may simply
    dislike the fill (thin book, wide spread, size under the market minimum,
    resolves within hours).

Two risk policies that are easy to get backwards:

  * `settings.max_order_usdc` bounds how much capital ONE ACTION PUTS AT RISK,
    so it applies to **BUYs only**. A SELL reduces risk. Capping sell proceeds
    at the same number made every exit of a position worth more than the cap
    permanently un-executable - silently disabling every stop-loss and
    take-profit while the UI claimed the account was protected. Sells are
    protected instead by (a) refusing to sell anything we cannot price and
    (b) a slippage floor sent with the order.
  * Resting limit BUY orders are not escrowed by the CLOB: the collateral
    balance still counts USDC that is already spoken for. Both the cash check
    and the per-market position cap therefore add open BUY exposure, and a
    failure to *read* that exposure blocks buying rather than counting as zero.

`execute_plan` rebuilds the plan from live state immediately before posting.
A preview approved 30 seconds ago is not evidence about now: the owner can
trade on the website, a market can stop accepting orders, and a position can
disappear.

Polymarket specifics that shape this code:
  * order **size is quantized to 2 decimals** for every supported tick size, so
    sell sizes are floored to 2dp. Asking to sell more shares than you hold by
    a rounding hair comes back as "not enough balance / allowance".
  * a limit price must be an exact integer multiple of the market's tick size.
    That is checked in `Decimal`, because in float `0.03 % 0.01` is 0.00999...
  * the authoritative tick size and minimum order size live on the CLOB **order
    book**, not on the gamma market record.
  * `place_market_order` uses `amount` (USDC) for BUY and `shares` for SELL;
    they are not interchangeable and the SDK rejects the wrong one.

Nothing here forecasts anything. A plan states mechanics only: what it costs,
what the position pays if that outcome wins, and what would make the exchange
or our own limits say no.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Literal, NamedTuple

from polymarket import Market, SecureClient

from polymarket_bot import portfolio
from polymarket_bot.config import Settings, load_settings
from polymarket_bot.markets import BookSnapshot, get_market_by_condition_id, is_tradable
from polymarket_bot.notify import Level, Notifier

Side = Literal["BUY", "SELL"]
Outcome = Literal["yes", "no"]


def _default_max_order_usdc() -> float:
    # Module-level convenience only. Enforcement always uses the Settings that
    # were passed in, so a caller with different limits is never shadowed here.
    try:
        return load_settings().max_order_usdc
    except Exception:
        return 5.0


#: Per-order USDC ceiling from the environment. Kept importable for scripts
#: that want to show the limit without building a Settings object.
MAX_ORDER_USDC: float = _default_max_order_usdc()

# Order size rounds to 2 decimals for *every* tick size Polymarket supports
# (see polymarket._internal.actions.orders.context._ROUNDING_BY_TICK), so the
# size grid is 0.01 shares regardless of how fine the price grid is.
_SIZE_QUANTUM = Decimal("0.01")

# Fallbacks when the CLOB book is unreachable and gamma has no value either.
_DEFAULT_TICK = 0.01
_DEFAULT_MIN_ORDER_SIZE = 5.0

# Warning thresholds - advisory only, never blockers.
_WIDE_SPREAD = 0.03  # 3 cents
_THIN_BOOK_MULTIPLE = 2.0  # want 2x the order resting on the side we cross
_SOON_DAYS = 1

# Below this the numbers are noise, not money.
_DUST_USDC = 0.01

# --- market-order slippage protection -------------------------------------
# A market order crosses the book and fills at whatever happens to be resting,
# so on a thin or fast-moving book it can print at a price nobody would accept
# on purpose. Every market order therefore carries a protective price derived
# from the touch: a SELL gets `min_price`, a BUY gets `max_price`, and the
# exchange refuses to fill past it (an FAK order simply stops matching).
#
# The tolerance is relative because prediction-market prices are probabilities:
# 10% of the touch is ~5.5c at 0.55 and ~0.3c at 0.03, which is the right shape.
_SLIPPAGE_TOLERANCE = 0.10
# ...but below ~10c a relative tolerance is smaller than one tick and therefore
# not expressible on the price grid, so always allow at least this many ticks of
# room. Without it the protective price rounds onto the touch itself and the
# order can never fill.
_MIN_SLIPPAGE_TICKS = 1


def _dec(value: float | int | str | Decimal) -> Decimal:
    # str() first: Decimal(0.03) is 0.0299999999999999988897769753748...
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _floor_shares(shares: float) -> float:
    """Floor to the exchange's 2dp size grid. Never rounds *up* - rounding up a
    sell is how you get an "insufficient balance" rejection on dust."""
    return float(_dec(shares).quantize(_SIZE_QUANTUM, rounding=ROUND_DOWN))


def _finite(value: float | None, digits: int = 6) -> float | None:
    """Round a number for output, or None if it is not a real number.

    `to_dict()` has to be JSON-safe and NaN/inf are not valid JSON, so a value
    that got contaminated is reported as "unknown" rather than as a number the
    reader would trust.
    """
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _short(token_id: str) -> str:
    # ASCII only: this string reaches Windows consoles that are not UTF-8.
    return token_id if len(token_id) <= 14 else f"{token_id[:6]}..{token_id[-4:]}"


class _Facts(NamedTuple):
    """Live book facts for one outcome token. Private: never returned to callers."""

    snapshot: BookSnapshot | None
    tick_size: float
    min_order_size: float
    error: str | None


class _OpenBuyExposure(NamedTuple):
    """USDC committed to resting limit BUY orders. Private.

    `error` is not "no exposure": it means we could not find out. Callers must
    treat it as a blocker on the buy side, never as zero.
    """

    total_usdc: float
    by_condition: dict[str, float]
    error: str | None


@dataclass
class OrderPlan:
    """A fully-priced, fully-checked order that has NOT been sent."""

    kind: Literal["market", "limit"]
    side: Side
    market_title: str
    condition_id: str
    token_id: str
    outcome_label: str
    usdc_amount: float | None  # BUY: what we spend / budget
    shares: float | None  # SELL size, or the size leg of a limit order
    limit_price: float | None
    est_price: float | None
    est_shares: float | None
    est_proceeds: float | None  # SELL only; a BUY has no proceeds, it has a payout
    min_order_size: float
    tick_size: float
    # Worst fill price the exchange may give us on a MARKET order: `min_price`
    # on a SELL, `max_price` on a BUY. None on limit orders (the limit price is
    # already the protection) and never None on an executable market order.
    protect_price: float | None = None
    # Estimated exchange fee for this order, in USDC. Some markets (notably
    # `sports_fees_v2`) charge the TAKER 5%, and a market order is always a
    # taker. Measured live: a $3.00 round trip on a 5% market moved cash by
    # -$0.17 while the fills alone accounted for -$0.01. Reporting P&L without
    # this is off by more than the P&L itself.
    est_fee: float | None = None
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def is_executable(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "side": self.side,
            "market_title": self.market_title,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome_label": self.outcome_label,
            "usdc_amount": self.usdc_amount,
            "shares": self.shares,
            "limit_price": self.limit_price,
            "est_price": self.est_price,
            "est_shares": self.est_shares,
            "est_proceeds": self.est_proceeds,
            "est_fee": self.est_fee,
            "min_order_size": self.min_order_size,
            "tick_size": self.tick_size,
            "protect_price": self.protect_price,
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "is_executable": self.is_executable(),
        }

    def to_text(self) -> str:
        """Plain ASCII with aligned labels: scannable in a terminal (including
        a non-UTF-8 Windows console) and unmangled when pasted into Telegram."""
        lines = [
            f"ORDER PLAN - {self.kind.upper()} {self.side}",
            f"  Market   : {self.market_title}",
            f"  Outcome  : '{self.outcome_label}'  (token {_short(self.token_id)})",
        ]

        if self.side == "BUY":
            if self.usdc_amount is not None:
                lines.append(f"  Spend    : ${self.usdc_amount:.2f} USDC")
            if self.kind == "limit" and self.limit_price is not None:
                if self.shares:
                    lines.append(
                        f"  Limit    : {self.shares:,.2f} shares @ {self.limit_price:.4f} (resting bid)"
                    )
                    lines.append(
                        f"  Locks    : ${self.shares * self.limit_price:.2f} USDC "
                        f"until filled or cancelled"
                    )
                else:
                    lines.append(f"  Limit    : {self.limit_price:.4f} (resting bid)")
            elif self.est_price is not None:
                lines.append(f"  Est fill : ~{self.est_price:.4f} per share")
            if self.est_shares is not None:
                lines.append(f"  Get      : ~{self.est_shares:,.2f} shares")
                lines.append(
                    f"  Payout   : ${self.est_shares:,.2f} if '{self.outcome_label}' wins, "
                    f"$0 if it loses"
                )
            if self.kind == "market" and self.protect_price is not None:
                lines.append(
                    f"  Ceiling  : will not pay above {self.protect_price:.4f} per share "
                    f"(slippage guard)"
                )
        else:
            if self.shares is not None:
                lines.append(f"  Sell     : {self.shares:,.2f} shares")
            if self.kind == "limit" and self.limit_price is not None:
                lines.append(f"  Limit    : {self.limit_price:.4f} per share (resting ask)")
            elif self.est_price is not None:
                lines.append(f"  Est fill : ~{self.est_price:.4f} per share")
            if self.est_proceeds is not None:
                lines.append(f"  Proceeds : ~${self.est_proceeds:.2f} USDC")
            if self.kind == "market" and self.protect_price is not None:
                lines.append(
                    f"  Floor    : will not fill below {self.protect_price:.4f} per share "
                    f"(slippage guard)"
                )

        if self.est_fee:
            lines.append(f"  Fee      : ~${self.est_fee:.2f} exchange fee (charged on top)")

        lines.append(
            f"  Exchange : tick {self.tick_size:g} | min order {self.min_order_size:g} shares"
        )

        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        for blocker in self.blockers:
            lines.append(f"  X BLOCKED: {blocker}")

        if self.blockers:
            lines.append(
                f"  STATUS   : REFUSED - {len(self.blockers)} blocker(s), nothing will be sent."
            )
        elif any("likely reject" in w for w in self.warnings):
            # "executable" sitting directly under "Polymarket will likely
            # reject it" reads as a contradiction, and a non-expert resolves it
            # by adding --execute and eating the rejection. We still allow the
            # attempt - the exchange, not this tool, is the authority on what
            # it accepts - but the status must not sound like a green light.
            lines.append(
                "  STATUS   : allowed, but the exchange will probably reject it - see the warning above."
            )
        else:
            lines.append("  STATUS   : executable - not sent yet; execute_plan() must be called.")
        return "\n".join(lines)


@dataclass
class TradeResult:
    """What the exchange actually did. `ok=True` with zero fill is normal for a
    limit order that rested on the book instead of matching."""

    ok: bool
    status: str | None
    order_id: str | None
    filled_shares: float
    filled_usdc: float
    avg_price: float | None
    tx_hashes: list[str]
    error: str | None
    plan: OrderPlan

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "order_id": self.order_id,
            "filled_shares": self.filled_shares,
            "filled_usdc": self.filled_usdc,
            "avg_price": self.avg_price,
            "tx_hashes": list(self.tx_hashes),
            "error": self.error,
            "plan": self.plan.to_dict(),
        }


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _resolve_outcome(market: Market, outcome: str):
    """Map "yes"/"no" - or the market's own label - onto a MarketOutcome.

    Real markets rarely say Yes/No: "Bitcoin Up or Down" labels them Up/Down,
    so a caller (or a Telegram user) typing `up` must work.
    """
    wanted = (outcome or "").strip().lower()
    yes, no = market.outcomes.yes, market.outcomes.no
    if wanted in ("yes", "y", "1"):
        return yes
    if wanted in ("no", "n", "0"):
        return no
    if yes.label and yes.label.strip().lower() == wanted:
        return yes
    if no.label and no.label.strip().lower() == wanted:
        return no
    raise ValueError(
        f"Unknown outcome {outcome!r}; this market offers "
        f"'{yes.label}' (yes) or '{no.label}' (no)."
    )


def _outcome_key(market: Market, token_id: str) -> Outcome:
    """Reverse lookup used when re-verifying a plan against a re-fetched market."""
    if market.outcomes.yes.token_id == token_id:
        return "yes"
    if market.outcomes.no.token_id == token_id:
        return "no"
    raise ValueError(f"Token {_short(token_id)} does not belong to market {market.condition_id}.")


def _facts(client: SecureClient, market: Market, token_id: str) -> _Facts:
    """Book snapshot + the exchange's own tick / minimum size, in one call.

    The CLOB book is authoritative for tick and minimum; the gamma market
    record is only a fallback because it can lag.
    """
    gamma_tick = float(market.trading.minimum_tick_size or 0) or _DEFAULT_TICK
    gamma_min = float(market.trading.minimum_order_size or 0) or _DEFAULT_MIN_ORDER_SIZE
    try:
        book = client.get_order_book(token_id=token_id)
    except Exception as exc:
        return _Facts(None, gamma_tick, gamma_min, f"{type(exc).__name__}: {exc}")

    # Build via from_book, never by hand: it defines bid_depth/ask_depth as the
    # shares reachable NEAR THE TOUCH. Summing the whole book here made every
    # thin-book warning dead code, because a market with 50,000 shares parked at
    # $0.01 looks infinitely deep while offering 50 shares you can actually hit.
    snapshot = BookSnapshot.from_book(token_id, book)
    return _Facts(
        snapshot=snapshot,
        tick_size=float(book.tick_size) or gamma_tick,
        min_order_size=float(book.min_order_size) or gamma_min,
        error=None,
    )


def _estimate_fee(
    market: Market, *, kind: str, shares: float | None, price: float | None
) -> tuple[float | None, str | None]:
    """(estimated fee in USDC, warning) for this order.

    Polymarket charges some markets a taker fee against the *cheaper side* of
    the contract - fee ~ rate * min(price, 1-price) * shares - so it costs the
    same whether you buy YES at 0.05 or NO at 0.95. A market order is always a
    taker; a resting limit order usually is not, which is why `taker_only`
    matters and why a limit order can be materially cheaper.

    Returns (None, None) when the market charges nothing, so callers can stay
    quiet rather than printing "fee: $0.00" on every order.
    """
    trading_cfg = market.trading
    schedule = getattr(trading_cfg, "fee_schedule", None)
    if not getattr(trading_cfg, "fees_enabled", False) or schedule is None:
        return None, None
    try:
        rate = float(schedule.rate)
    except (TypeError, ValueError):
        return None, None
    if rate <= 0 or shares is None or price is None or not (0 < price < 1):
        return None, None

    taker_only = bool(getattr(schedule, "taker_only", False))
    if taker_only and kind == "limit":
        return 0.0, (
            f"This market charges takers {rate * 100:g}%. A resting limit order normally "
            f"avoids it - one more reason to prefer a limit here."
        )

    fee = rate * min(price, 1.0 - price) * shares
    notional = price * shares
    pct = (fee / notional * 100.0) if notional > 0 else 0.0
    warning = None
    if fee >= 0.01:
        warning = (
            f"Exchange fee ~${fee:.2f} ({pct:.1f}% of ${notional:.2f}) - this market charges "
            f"takers {rate * 100:g}% and a market order is always a taker. Budget for it on the "
            f"way out too; a measured $3.00 round trip here cost about $0.16 in fees on top of "
            f"the spread, so a short hold starts well behind."
        )
    return round(fee, 6), warning


def _check_limit_price(limit_price: float, tick_size: float) -> str | None:
    """Return a blocker message, or None if the price is on the tick grid.

    Decimal throughout: the exchange divides price by tick and demands an
    integer, and float modulo cannot answer that question reliably.
    """
    try:
        price = _dec(limit_price)
        tick = _dec(tick_size)
    except (InvalidOperation, ValueError):
        return f"limit price {limit_price!r} is not a number."
    if tick <= 0:
        return f"unusable tick size {tick_size!r} for this market."
    if price <= 0 or price >= 1:
        return f"limit price {price} must be strictly between 0 and 1 (it is a probability)."
    if price % tick != 0:
        return f"limit price {price} is not a multiple of this market's tick size {tick}."
    return None


def _snap_to_tick(price: float, tick_size: float, *, up: bool) -> float | None:
    """Snap `price` onto the market's tick grid, or None if it cannot be.

    The exchange divides an order price by the tick and demands an integer, so
    a protective price that is off-grid is rejected outright - which would turn
    the guard into an exception on the write path. Decimal throughout, because
    in float `0.495 // 0.01` is not what anyone wants.
    """
    try:
        tick = _dec(tick_size)
        if tick <= 0:
            return None
        steps = (_dec(price) / tick).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
        snapped = steps * tick
        if not snapped.is_finite():
            return None
        # The SDK requires tick <= price <= 1 - tick; anything else is unusable.
        if snapped < tick or snapped > _dec(1) - tick:
            return None
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return float(snapped)


def _protective_price(
    facts: _Facts, *, side: Side, reference_price: float | None = None
) -> tuple[float | None, str | None]:
    """Worst fill price we will accept on a market order, or (None, reason).

    Anchored to the price this order is actually expected to fill at, then
    widened by `_SLIPPAGE_TOLERANCE` (never by less than `_MIN_SLIPPAGE_TICKS`)
    and snapped onto the tick grid.

    The anchor is deliberately the WORSE of the touch and `reference_price`
    (the exchange's own estimate for this size). Anchoring on the touch alone
    is a trap: a book of [0.90 x 1 share, 0.50 x 5000 shares] has a best bid of
    0.90, so a 1000-share exit would get a 0.81 floor against a realistic fill
    near 0.50 - an order that can only ever fill 1 share, re-sent every sweep
    forever while a stop-loss quietly fails to protect anything.

    A one-sided or unreadable book returns a reason instead of a price. The
    tolerance is NEVER widened to rescue such a book: "no price to protect
    against" is a refusal, not an invitation to send an unprotected order.
    """
    if facts.error is not None:
        return None, f"the order book could not be read ({facts.error})"
    snapshot = facts.snapshot
    if snapshot is None:
        return None, "no order book snapshot is available"

    touch = snapshot.best_bid if side == "SELL" else snapshot.best_ask
    label = "bids" if side == "SELL" else "asks"
    if touch is None:
        return None, f"there are no {label} resting on this book"
    if not math.isfinite(touch) or not (0 < touch < 1):
        return None, f"the best {label[:-1]} {touch} is not a usable price"

    anchor = touch
    if reference_price is not None and math.isfinite(reference_price) and 0 < reference_price < 1:
        # Worse-of: for a SELL that is the lower price, for a BUY the higher.
        anchor = min(touch, reference_price) if side == "SELL" else max(touch, reference_price)

    tick = facts.tick_size if facts.tick_size > 0 else _DEFAULT_TICK
    room = max(anchor * _SLIPPAGE_TOLERANCE, _MIN_SLIPPAGE_TICKS * tick)
    if side == "SELL":
        # Snap DOWN so rounding always widens the guard rather than tightening
        # it onto the anchor, and clamp to the grid minimum.
        price = _snap_to_tick(max(anchor - room, tick), tick, up=False)
        # A floor above the anchor is not protection, it is an order that can
        # never fill. It means the anchor itself is off this market's grid, so
        # say we cannot protect it instead of returning a number that lies.
        if price is not None and price > anchor:
            price = None
    else:
        price = _snap_to_tick(min(anchor + room, 1.0 - tick), tick, up=True)
        if price is not None and price < anchor:
            price = None

    if price is None:
        return None, (
            f"no usable on-grid protective price exists for a touch of {touch:.4f} "
            f"on a {tick:g} tick grid"
        )
    return price, None


def _open_buy_exposure(client: SecureClient) -> _OpenBuyExposure:
    """USDC sitting in unfilled limit BUY orders, in total and per market.

    The CLOB does not escrow collateral for a resting bid, so
    `get_balance_allowance` still reports that money as free. Without this,
    N limit buys that each pass the cash check and the per-market position cap
    individually can blow through both together.
    """
    try:
        orders = list(client.list_open_orders().iter_items())
    except Exception as exc:
        return _OpenBuyExposure(0.0, {}, f"{type(exc).__name__}: {exc}")

    total = 0.0
    by_condition: dict[str, float] = {}
    for order in orders:
        try:
            if str(getattr(order, "side", "") or "").upper() != "BUY":
                continue
            price = float(order.price)
            remaining = float(order.original_size) - float(order.size_matched)
        except (AttributeError, TypeError, ValueError):
            # An order we cannot measure is exposure we cannot bound.
            return _OpenBuyExposure(0.0, {}, "an open order had an unreadable price or size")
        if not math.isfinite(price) or not math.isfinite(remaining):
            return _OpenBuyExposure(0.0, {}, "an open order had a non-finite price or size")
        if price <= 0 or remaining <= 0:
            continue
        notional = price * remaining
        total += notional
        key = str(order.condition_id or getattr(order, "market", "") or "").lower()
        by_condition[key] = by_condition.get(key, 0.0) + notional

    return _OpenBuyExposure(
        round(total, 6), {k: round(v, 6) for k, v in by_condition.items()}, None
    )


def _market_cost_basis(
    client: SecureClient, condition_id: str, *, open_buy_usdc: float = 0.0
) -> float:
    """USDC already committed to this market, both outcomes summed.

    The position cap is per-market, so holding Yes and No legs of the same
    condition still counts once against `max_position_usdc`. `open_buy_usdc` is
    the notional of resting limit bids on this market (see
    `_open_buy_exposure`): unfilled, but already promised to it.
    """
    wanted = (condition_id or "").lower()
    total = float(open_buy_usdc)
    for view in portfolio.get_positions(client, include_resolved=True):
        if view.condition_id.lower() == wanted:
            total += view.cost_basis
    return round(total, 6)


def _days_left(market: Market) -> int | None:
    end = market.state.end_date
    if end is None:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - datetime.now(timezone.utc)).days


def _liquidity_warnings(facts: _Facts, *, side: Side, size_shares: float | None) -> list[str]:
    """Spread / depth warnings. Advisory: a thin book costs you money, it is
    not a reason to refuse an order the owner asked for."""
    out: list[str] = []
    if facts.error is not None:
        out.append(f"Could not read the order book ({facts.error}); fill quality is unknown.")
        return out
    snapshot = facts.snapshot
    if snapshot is None:
        return out

    spread = snapshot.spread
    if spread is None:
        out.append("One side of the book is empty - there may be nothing to trade against.")
    elif spread > _WIDE_SPREAD:
        out.append(f"Wide spread ({spread * 100:.1f}c) - crossing it costs real money on a round trip.")

    if size_shares:
        # Crossing the book eats the opposite side: a buy takes asks, a sell hits bids.
        depth = snapshot.ask_depth if side == "BUY" else snapshot.bid_depth
        label = "asks" if side == "BUY" else "bids"
        if depth < size_shares:
            out.append(
                f"Thin book: only {depth:,.0f} shares resting on the {label} vs "
                f"{size_shares:,.2f} wanted - expect partial fill or slippage."
            )
        elif depth < size_shares * _THIN_BOOK_MULTIPLE:
            out.append(
                f"Shallow book: {depth:,.0f} shares on the {label} for a {size_shares:,.2f} "
                f"share order - you are a large part of it."
            )
    return out


def _horizon_warning(market: Market) -> str | None:
    days = _days_left(market)
    if days is None:
        return None
    if days < 0:
        return "End date has already passed - this market is resolving or closed."
    if days <= _SOON_DAYS:
        return f"Resolves in under {days + 1} day - very little time for the price to move your way."
    return None


def _min_size_warning(shares: float | None, min_order_size: float) -> str | None:
    if shares is None or min_order_size <= 0 or shares >= min_order_size:
        return None
    return (
        f"~{shares:,.2f} shares is below this market's {min_order_size:g}-share minimum - "
        f"Polymarket will likely reject it. Increase the size or pick a lower-priced outcome."
    )


def _estimate(
    client: SecureClient,
    token_id: str,
    *,
    side: Side,
    amount: float | None = None,
    shares: float | None = None,
) -> tuple[float | None, str | None]:
    """Average fill price for a market order, or (None, reason)."""
    try:
        if side == "BUY":
            price = client.estimate_market_price(
                token_id=token_id, side="BUY", amount=amount, order_type="FAK"
            )
        else:
            price = client.estimate_market_price(
                token_id=token_id, side="SELL", shares=shares, order_type="FAK"
            )
        value = float(price)
        return (value, None) if value > 0 else (None, "exchange returned a zero price estimate")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _notify(notifier: Notifier | None, message: str, level: Level = "info") -> None:
    # A broken sink must never make a placed order look like a failure.
    if notifier is None:
        return
    try:
        notifier.send(message, level=level)
    except Exception:
        pass


# --------------------------------------------------------------------------
# plan builders (read-only)
# --------------------------------------------------------------------------


def build_buy_plan(
    client: SecureClient,
    settings: Settings,
    *,
    market: Market,
    outcome: Outcome,
    usdc_amount: float,
    limit_price: float | None = None,
) -> OrderPlan:
    """Price and vet a BUY of `usdc_amount` USDC of one outcome. Sends nothing.

    Buying NO is a first-class path: `outcome="no"` resolves
    `market.outcomes.no.token_id` and is checked identically to YES. Passing
    the market's own label ("Up", "Down", ...) works too.

    With `limit_price` this becomes a resting GTC bid: the size leg is
    `usdc_amount / limit_price` floored to the exchange's 2dp size grid, so the
    order never locks up more than the budget you asked for.
    """
    target = _resolve_outcome(market, outcome)
    token_id = target.token_id
    warnings: list[str] = []
    blockers: list[str] = []

    kind: Literal["market", "limit"] = "limit" if limit_price is not None else "market"
    amount = float(usdc_amount)

    if not token_id:
        # Nothing else can be checked without a token; return an inert plan.
        return OrderPlan(
            kind=kind,
            side="BUY",
            market_title=market.question or market.slug or str(market.condition_id),
            condition_id=str(market.condition_id or ""),
            token_id="",
            outcome_label=target.label,
            usdc_amount=_finite(amount),
            shares=None,
            limit_price=limit_price,
            est_price=None,
            est_shares=None,
            est_proceeds=None,
            min_order_size=_DEFAULT_MIN_ORDER_SIZE,
            tick_size=_DEFAULT_TICK,
            warnings=warnings,
            blockers=[f"Market has no tradable '{target.label}' token."],
        )

    facts = _facts(client, market, token_id)

    # NaN slips through every comparison below ("nan > 5.0" is False), so it
    # would pass the cap unchecked. Reject it before anything reads it.
    if not math.isfinite(amount):
        blockers.append(f"Order amount must be a finite number; got {usdc_amount!r}.")
    elif amount <= 0:
        blockers.append("Order amount must be greater than $0.")
    if amount > settings.max_order_usdc:
        blockers.append(
            f"${amount:.2f} is over the ${settings.max_order_usdc:.2f} per-order cap "
            f"(POLYMARKET_MAX_ORDER_USDC)."
        )
    if not is_tradable(market):
        blockers.append("Market is not accepting orders right now.")

    if limit_price is not None:
        problem = _check_limit_price(limit_price, facts.tick_size)
        if problem:
            blockers.append(problem)

    # --- USDC already promised to resting bids ------------------------------
    # Not escrowed by the CLOB, so it is still in the collateral balance and
    # still absent from the position list. It has to be counted in both checks
    # below or N individually-legal limit buys bypass both caps together.
    exposure = _open_buy_exposure(client)
    if exposure.error is not None:
        blockers.append(
            f"Could not read resting orders to measure committed USDC ({exposure.error}) - "
            f"refusing to buy against an unknown exposure."
        )
    market_key = str(market.condition_id or "").lower()
    committed_here = exposure.by_condition.get(market_key, 0.0)

    # --- cash, respecting the reserve and resting bids -----------------------
    try:
        cash = portfolio.get_cash_balance(client)
        spendable = cash - settings.min_cash_reserve_usdc - exposure.total_usdc
        if amount > spendable + 1e-9:
            committed_note = (
                f" and ${exposure.total_usdc:.2f} committed to resting bids"
                if exposure.total_usdc > _DUST_USDC
                else ""
            )
            blockers.append(
                f"Insufficient cash: ${amount:.2f} needed, ${max(spendable, 0.0):.2f} spendable "
                f"(${cash:.2f} balance less a ${settings.min_cash_reserve_usdc:.2f} reserve"
                f"{committed_note})."
            )
    except Exception as exc:
        # Unverifiable cash is a blocker, not a warning: spending money we
        # could not confirm we have is exactly what the limits exist to stop.
        blockers.append(f"Could not read the USDC balance ({type(exc).__name__}: {exc}).")

    # --- per-market position cap -------------------------------------------
    try:
        existing = _market_cost_basis(client, market_key, open_buy_usdc=committed_here)
        resting_note = (
            f" (${committed_here:.2f} of it resting unfilled)" if committed_here > _DUST_USDC else ""
        )
        if existing + amount > settings.max_position_usdc + 1e-9:
            blockers.append(
                f"Position cap: ${existing:.2f} already committed to this market{resting_note} "
                f"+ ${amount:.2f} = ${existing + amount:.2f}, over the "
                f"${settings.max_position_usdc:.2f} limit (POLYMARKET_MAX_POSITION_USDC)."
            )
        elif existing > _DUST_USDC:
            warnings.append(
                f"You already have ${existing:.2f} committed to this market{resting_note}."
            )
    except Exception as exc:
        blockers.append(f"Could not read existing positions ({type(exc).__name__}: {exc}).")

    # --- sizing -------------------------------------------------------------
    shares: float | None = None
    est_price: float | None = None
    est_shares: float | None = None

    if limit_price is not None:
        est_price = float(limit_price)
        # Floor the size so shares * price never exceeds the stated budget.
        shares = _floor_shares(amount / est_price) if est_price > 0 else 0.0
        est_shares = shares
        if shares <= 0:
            blockers.append(
                f"${amount:.2f} at {est_price:.4f} buys less than 0.01 shares - nothing to send."
            )
        if facts.snapshot and facts.snapshot.best_ask is not None and est_price >= facts.snapshot.best_ask:
            warnings.append(
                f"Bid of {est_price:.4f} is at or above the best ask {facts.snapshot.best_ask:.4f} - "
                f"this will cross and fill immediately, not rest on the book."
            )
    elif math.isfinite(amount) and amount > 0:
        est_price, problem = _estimate(client, token_id, side="BUY", amount=amount)
        if est_price is None:
            fallback = float(target.price) if target.price is not None else None
            # `is not None`, not truthiness: 0.0 is a real price for a settled
            # outcome, and reporting it beats claiming we have nothing.
            warnings.append(
                f"Could not estimate a fill price ({problem}); "
                + (
                    f"using the last published price {fallback:.4f}."
                    if fallback is not None
                    else "no price available."
                )
            )
            est_price = fallback
        if est_price:
            est_shares = round(amount / est_price, 2)

    est_fee, fee_warning = _estimate_fee(market, kind=kind, shares=est_shares, price=est_price)
    if fee_warning:
        warnings.append(fee_warning)

    # --- slippage ceiling (market orders only) -------------------------------
    # A limit buy is already protected by its own price. A market buy is not:
    # without max_price it pays whatever the thinnest ask on the book asks for.
    protect_price: float | None = None
    if kind == "market":
        protect_price, problem = _protective_price(
            facts, side="BUY", reference_price=est_price
        )
        if protect_price is None:
            blockers.append(
                f"Cannot set a slippage ceiling for this market BUY ({problem}) - refusing. "
                f"An unprotected market order fills at any price the book offers."
            )
        elif est_price is not None and est_price > protect_price:
            # Mirror of the SELL floor: an order whose own ceiling forbids its
            # own estimate cannot fill, so refuse rather than send a stub order.
            blockers.append(
                f"This buy cannot fill: the estimated price for ${amount:,.2f} is "
                f"{est_price:.4f}, above its own {protect_price:.4f} slippage ceiling. "
                f"The top of book is too thin for this size - buy a smaller amount, or "
                f"use a limit order at a price you are willing to pay."
            )

    # --- advisory -----------------------------------------------------------
    warnings.extend(_liquidity_warnings(facts, side="BUY", size_shares=est_shares))
    note = _min_size_warning(est_shares, facts.min_order_size)
    if note:
        warnings.append(note)
    horizon = _horizon_warning(market)
    if horizon:
        warnings.append(horizon)

    return OrderPlan(
        kind=kind,
        side="BUY",
        market_title=market.question or market.slug or str(market.condition_id),
        condition_id=str(market.condition_id or ""),
        token_id=str(token_id),
        outcome_label=target.label,
        # Keep to_dict() JSON-safe: NaN/inf are not valid JSON numbers.
        usdc_amount=_finite(amount),
        shares=shares,
        limit_price=float(limit_price) if limit_price is not None else None,
        est_price=_finite(est_price),
        est_shares=est_shares,
        est_proceeds=None,  # a buy has no proceeds; the payout line covers it
        min_order_size=facts.min_order_size,
        tick_size=facts.tick_size,
        protect_price=protect_price,
        est_fee=est_fee,
        warnings=warnings,
        blockers=blockers,
    )


def build_sell_plan(
    client: SecureClient,
    settings: Settings,
    *,
    market: Market,
    outcome: Outcome,
    shares: float | None = None,
    fraction: float | None = None,
    limit_price: float | None = None,
) -> OrderPlan:
    """Price and vet a SELL of an outcome you hold. Sends nothing.

    Exactly one of `shares` / `fraction` (`fraction=1.0` means sell everything).
    The size is always resolved against the **live** position - positions move
    outside this bot - and the result is floored to the exchange's 2dp size
    grid so dust can never turn into a "not enough balance" rejection.

    `settings.max_order_usdc` is NOT applied here. It caps capital put at risk,
    and a sell removes risk; capping proceeds instead made every exit of a
    position worth more than the cap permanently un-executable, which silently
    disabled every stop-loss and take-profit the monitor tried to fire. The
    protections that replace it are both in this function: refuse any sell we
    cannot price (rather than treating a failed estimate as $0.00), and attach
    a slippage floor so a thin book cannot fill at a garbage price.
    """
    target = _resolve_outcome(market, outcome)
    token_id = target.token_id
    warnings: list[str] = []
    blockers: list[str] = []
    kind: Literal["market", "limit"] = "limit" if limit_price is not None else "market"

    title = market.question or market.slug or str(market.condition_id)
    condition_id = str(market.condition_id or "")

    def inert(blocker: str, *, size: float | None = None, facts: _Facts | None = None) -> OrderPlan:
        return OrderPlan(
            kind=kind,
            side="SELL",
            market_title=title,
            condition_id=condition_id,
            token_id=str(token_id or ""),
            outcome_label=target.label,
            usdc_amount=None,
            shares=size,
            limit_price=float(limit_price) if limit_price is not None else None,
            est_price=None,
            est_shares=size,
            est_proceeds=None,
            min_order_size=facts.min_order_size if facts else _DEFAULT_MIN_ORDER_SIZE,
            tick_size=facts.tick_size if facts else _DEFAULT_TICK,
            protect_price=None,
            warnings=warnings,
            blockers=[*blockers, blocker],
        )

    if not token_id:
        return inert(f"Market has no tradable '{target.label}' token.")

    if (shares is None) == (fraction is None):
        return inert("Specify exactly one of shares or fraction (fraction=1.0 sells everything).")
    # NaN compares False against everything, so it has to be rejected by name
    # rather than by a range check.
    if fraction is not None and (not math.isfinite(fraction) or not (0 < fraction <= 1)):
        return inert(f"fraction must be a finite number in (0, 1]; got {fraction}.")
    if shares is not None and (not math.isfinite(shares) or shares <= 0):
        return inert(f"shares must be a finite number greater than 0; got {shares}.")

    facts = _facts(client, market, str(token_id))

    # --- live holding (never trust a cached position) ------------------------
    try:
        position = portfolio.find_position(client, token_id=str(token_id))
    except Exception as exc:
        return inert(f"Could not read positions ({type(exc).__name__}: {exc}).", facts=facts)

    if position is None or position.shares <= 0:
        return inert(
            f"You hold no '{target.label}' shares in this market - nothing to sell.", facts=facts
        )

    held = position.shares
    requested = held * float(fraction) if fraction is not None else float(shares or 0.0)
    size = _floor_shares(requested)
    # Flooring the *holding* too: the exchange will not accept 70.5936 as a size.
    size = min(size, _floor_shares(held))

    if size <= 0:
        return inert(
            f"Requested size rounds to 0 shares (you hold {held:,.4f}; "
            f"the exchange trades in 0.01-share steps).",
            facts=facts,
        )
    if requested > held + 1e-9:
        return inert(
            f"Insufficient shares: {requested:,.2f} requested, {held:,.4f} held.",
            size=size,
            facts=facts,
        )

    if position.is_resolved:
        warnings.append(
            "This market has settled - redeem the position instead of selling it; "
            "a settled loser is worth $0 either way."
        )

    if not is_tradable(market):
        blockers.append("Market is not accepting orders right now.")

    if limit_price is not None:
        problem = _check_limit_price(limit_price, facts.tick_size)
        if problem:
            blockers.append(problem)

    # --- pricing -------------------------------------------------------------
    est_price: float | None = None
    if limit_price is not None:
        est_price = float(limit_price)
        if facts.snapshot and facts.snapshot.best_bid is not None and est_price <= facts.snapshot.best_bid:
            warnings.append(
                f"Ask of {est_price:.4f} is at or below the best bid {facts.snapshot.best_bid:.4f} - "
                f"this will cross and fill immediately, not rest on the book."
            )
    else:
        est_price, problem = _estimate(client, str(token_id), side="SELL", shares=size)
        if est_price is None:
            # FAIL CLOSED. `_estimate` returns None both when the call raises
            # and when the exchange quotes zero, and `position.cur_price` is
            # 0.0 (never None) whenever the data-api omitted it - so the old
            # "fallback price" could be 0.0, which quietly made a $275 exit look
            # like $0.00 of proceeds. Walk sources we can actually point at, in
            # descending order of trustworthiness, and refuse if none is
            # positive rather than inventing a number.
            sources: tuple[tuple[str, float | None], ...] = (
                ("the best bid", facts.snapshot.best_bid if facts.snapshot else None),
                ("the last known position price", position.cur_price),
                (
                    "the market's published price",
                    float(target.price) if target.price is not None else None,
                ),
            )
            for label, candidate in sources:
                if candidate is not None and math.isfinite(candidate) and candidate > 0:
                    warnings.append(
                        f"Could not estimate a fill price ({problem}); pricing this sell off "
                        f"{label}, {candidate:.4f}."
                    )
                    est_price = float(candidate)
                    break
            else:
                blockers.append(
                    f"Cannot price this sell ({problem}) and neither the book nor the position "
                    f"offers a price above zero - refusing rather than acting on a $0.00 estimate."
                )

    est_proceeds = _finite(size * est_price) if est_price is not None else None

    # NOTE: no per-order USDC cap on the sell side, by policy. See the docstring
    # above and the module header - `max_order_usdc` bounds capital put at risk,
    # and selling reduces risk. Do not reintroduce a proceeds cap here: with the
    # defaults (order cap $5, position cap $10) it makes any position worth more
    # than $5 impossible to exit, which disables every exit rule in the monitor.

    # --- slippage floor (market orders only) ---------------------------------
    # A limit sell is already protected by its own price. A market sell is not:
    # without min_price it fills against whatever is resting, however bad.
    protect_price: float | None = None
    if kind == "market":
        protect_price, problem = _protective_price(
            facts, side="SELL", reference_price=est_price
        )
        if protect_price is None:
            blockers.append(
                f"Cannot set a slippage floor for this market SELL ({problem}) - refusing. "
                f"An unprotected market order fills at any price the book offers."
            )
        elif est_price is not None and est_price < protect_price:
            # An order whose own floor forbids its own estimate cannot fill.
            # This is a blocker, not a warning: sending it would fill a token
            # sliver and leave the rest unsold while the monitor retries
            # forever, which is exactly how a stop-loss silently stops working.
            blockers.append(
                f"This sell cannot fill: the estimated price for {size:,.2f} shares is "
                f"{est_price:.4f}, below its own {protect_price:.4f} slippage floor. The "
                f"top of book is not deep enough for this size - sell a smaller amount, "
                f"or use a limit order at a price you are willing to accept."
            )

    est_fee, fee_warning = _estimate_fee(market, kind=kind, shares=size, price=est_price)
    if fee_warning:
        warnings.append(fee_warning)

    warnings.extend(_liquidity_warnings(facts, side="SELL", size_shares=size))
    note = _min_size_warning(size, facts.min_order_size)
    if note:
        warnings.append(note)
    if size < held - 0.005:
        warnings.append(f"Leaves {held - size:,.2f} shares still held ({held:,.4f} before this sell).")
    horizon = _horizon_warning(market)
    if horizon:
        warnings.append(horizon)

    return OrderPlan(
        kind=kind,
        side="SELL",
        market_title=title,
        condition_id=condition_id,
        token_id=str(token_id),
        outcome_label=target.label,
        usdc_amount=None,
        shares=size,
        limit_price=float(limit_price) if limit_price is not None else None,
        est_price=_finite(est_price),
        est_shares=size,
        est_proceeds=est_proceeds,
        min_order_size=facts.min_order_size,
        tick_size=facts.tick_size,
        protect_price=protect_price,
        est_fee=est_fee,
        warnings=warnings,
        blockers=blockers,
    )


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


def _failed(plan: OrderPlan, error: str) -> TradeResult:
    return TradeResult(
        ok=False,
        status=None,
        order_id=None,
        filled_shares=0.0,
        filled_usdc=0.0,
        avg_price=None,
        tx_hashes=[],
        error=error,
        plan=plan,
    )


def _rebuild(client: SecureClient, settings: Settings, plan: OrderPlan) -> OrderPlan:
    """Re-run the whole plan against freshly fetched state.

    This is the gate, not a re-quote: `execute_plan` still sends the amounts
    the caller approved. It exists because between preview and confirmation the
    owner can trade on the website, cash can drop, and a market can close.

    The one number `execute_plan` does take from here is `protect_price` - a
    slippage guard is only a guard if it came from the current book.
    """
    market = get_market_by_condition_id(client, plan.condition_id)
    outcome = _outcome_key(market, plan.token_id)
    if plan.side == "BUY":
        if plan.usdc_amount is None:
            raise ValueError("BUY plan has no usdc_amount.")
        return build_buy_plan(
            client,
            settings,
            market=market,
            outcome=outcome,
            usdc_amount=plan.usdc_amount,
            limit_price=plan.limit_price,
        )
    if plan.shares is None:
        raise ValueError("SELL plan has no share size.")
    return build_sell_plan(
        client,
        settings,
        market=market,
        outcome=outcome,
        shares=plan.shares,
        limit_price=plan.limit_price,
    )


def _read_fill(plan: OrderPlan, response: object) -> tuple[float, float, float | None]:
    """(filled_shares, filled_usdc, avg_price) from an accepted order.

    making/taking follow the signed order's maker/taker legs: a BUY offers USDC
    and takes shares, a SELL offers shares and takes USDC.
    """
    making = float(getattr(response, "making_amount", 0) or 0)
    taking = float(getattr(response, "taking_amount", 0) or 0)
    if plan.side == "BUY":
        filled_usdc, filled_shares = making, taking
    else:
        filled_shares, filled_usdc = making, taking

    avg: float | None = None
    if filled_shares > 0 and filled_usdc > 0:
        candidate = filled_usdc / filled_shares
        # A share price outside (0, 1] means the units are not what we think;
        # report no average rather than an invented one.
        avg = round(candidate, 6) if 0 < candidate <= 1 else None
    return round(filled_shares, 6), round(filled_usdc, 6), avg


def execute_plan(
    client: SecureClient,
    settings: Settings,
    plan: OrderPlan,
    *,
    notifier: Notifier | None = None,
) -> TradeResult:
    """Send the order described by `plan`. THE ONLY WRITE PATH IN THIS MODULE.

    Refuses if the plan has blockers, and refuses again if re-reading live
    state produces blockers. SDK failures come back as
    `TradeResult(ok=False, error=...)` rather than exceptions, so a caller
    sweeping many positions is never aborted by one bad market.
    """
    if plan.blockers:
        reason = "; ".join(plan.blockers)
        _notify(notifier, f"Refused {plan.side} '{plan.outcome_label}': {reason}", "alert")
        return _failed(plan, f"Refused (plan blockers): {reason}")

    # Re-verify: everything the plan asserted could have changed since.
    try:
        fresh = _rebuild(client, settings, plan)
    except Exception as exc:
        reason = f"Could not re-verify against live state ({type(exc).__name__}: {exc})."
        _notify(notifier, f"Refused {plan.side} '{plan.outcome_label}': {reason}", "alert")
        return _failed(plan, reason)

    if fresh.blockers:
        reason = "; ".join(fresh.blockers)
        _notify(notifier, f"Refused {plan.side} '{plan.outcome_label}': state changed - {reason}", "alert")
        return _failed(plan, f"Refused (live re-check): {reason}")

    for warning in fresh.warnings:
        _notify(notifier, warning, "info")

    # The slippage guard comes from `fresh`, not from `plan`: a protective price
    # derived from a book we read minutes ago is not protection. `_rebuild` just
    # re-read the touch, and a market plan with no protective price would have
    # produced a blocker above - so this is belt-and-braces, not a live path.
    protect_price = fresh.protect_price
    if plan.kind == "market" and protect_price is None:
        reason = "no slippage guard could be derived from the live book."
        _notify(notifier, f"Refused {plan.side} '{plan.outcome_label}': {reason}", "alert")
        return _failed(plan, f"Refused (no slippage guard): {reason}")

    size_note = (
        f"${plan.usdc_amount:.2f}" if plan.side == "BUY" and plan.usdc_amount is not None
        else f"{plan.shares:,.2f} shares" if plan.shares is not None
        else "?"
    )
    guard_note = ""
    if plan.kind == "market" and protect_price is not None:
        guard = "floor" if plan.side == "SELL" else "ceiling"
        guard_note = f" ({guard} {protect_price:.4f})"
    _notify(
        notifier,
        f"Sending {plan.kind} {plan.side} {size_note} of '{plan.outcome_label}'{guard_note} "
        f"- {plan.market_title}",
        "trade",
    )

    try:
        if plan.kind == "limit":
            if plan.limit_price is None or not plan.shares:
                return _failed(plan, "Limit plan is missing a price or a size.")
            response = client.place_limit_order(
                token_id=plan.token_id,
                price=plan.limit_price,
                size=plan.shares,
                side=plan.side,
                post_only=False,
            )
        elif plan.side == "BUY":
            if plan.usdc_amount is None:
                return _failed(plan, "Market BUY plan is missing a USDC amount.")
            # BUY market orders are denominated in USDC (`amount`), not shares.
            # `max_price` caps what we will pay per share; the exchange rejects
            # anything worse, so an FAK order stops matching instead of paying it.
            response = client.place_market_order(
                token_id=plan.token_id,
                side="BUY",
                amount=plan.usdc_amount,
                max_price=protect_price,
                order_type="FAK",
            )
        else:
            if not plan.shares:
                return _failed(plan, "Market SELL plan is missing a share size.")
            # SELL market orders are denominated in shares. `min_price` is the
            # slippage floor: below it the order stops matching rather than
            # dumping into whatever thin bid happens to be resting.
            response = client.place_market_order(
                token_id=plan.token_id,
                side="SELL",
                shares=plan.shares,
                min_price=protect_price,
                order_type="FAK",
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _notify(notifier, f"Order failed ({plan.market_title}): {error}", "error")
        return _failed(plan, error)

    # OrderResponse is AcceptedOrder | RejectedOrder - the rejected branch has
    # .code/.message and none of the fill fields.
    if not getattr(response, "ok", False):
        code = getattr(response, "code", None)
        message = getattr(response, "message", None) or getattr(response, "error_msg", None) or "rejected"
        error = f"{code}: {message}" if code else str(message)
        _notify(notifier, f"Order rejected ({plan.market_title}): {error}", "error")
        return _failed(plan, error)

    filled_shares, filled_usdc, avg_price = _read_fill(plan, response)
    status = str(getattr(response, "status", "") or "") or None
    result = TradeResult(
        ok=True,
        status=status,
        order_id=str(getattr(response, "order_id", "") or "") or None,
        filled_shares=filled_shares,
        filled_usdc=filled_usdc,
        avg_price=avg_price,
        tx_hashes=[str(h) for h in getattr(response, "transactions_hashes", ()) or ()],
        error=None,
        plan=plan,
    )

    price_note = f" @ ~{avg_price:.4f}" if avg_price is not None else ""
    if filled_shares <= 0:
        # Normal for a limit order that rested; a FAK market order matching
        # nothing also lands here and is worth saying out loud.
        detail = "resting on the book, unfilled" if plan.kind == "limit" else "no fill (nothing matched)"
        _notify(notifier, f"Order {status or 'accepted'} - {detail}: {plan.market_title}", "trade")
    else:
        partial = ""
        if plan.side == "BUY" and plan.usdc_amount and filled_usdc < plan.usdc_amount - _DUST_USDC:
            partial = f" (partial: ${filled_usdc:.2f} of ${plan.usdc_amount:.2f})"
        elif plan.side == "SELL" and plan.shares and filled_shares < plan.shares - 0.005:
            partial = f" (partial: {filled_shares:,.2f} of {plan.shares:,.2f} shares)"
        _notify(
            notifier,
            f"{plan.side} filled {filled_shares:,.2f} '{plan.outcome_label}' shares "
            f"for ${filled_usdc:.2f}{price_note}{partial} - {plan.market_title}",
            "trade",
        )
    return result


# --------------------------------------------------------------------------
# open orders
# --------------------------------------------------------------------------


def list_open_orders(client: SecureClient) -> list[dict]:
    """Every resting order on the account, JSON-safe.

    Resting limit orders are how a take-profit survives the bot being offline,
    so this is the ground truth for "what is still working for me".
    """
    out: list[dict] = []
    for order in client.list_open_orders().iter_items():
        original = float(order.original_size)
        matched = float(order.size_matched)
        out.append(
            {
                "order_id": order.id,
                "condition_id": str(order.condition_id),
                "token_id": str(order.token_id),
                "outcome": order.outcome,
                "side": order.side,
                "price": float(order.price),
                "original_size": round(original, 6),
                "size_matched": round(matched, 6),
                "size_remaining": round(original - matched, 6),
                "notional_usdc": round((original - matched) * float(order.price), 6),
                "order_type": order.order_type,
                "status": order.status,
                "created_at": order.created_at.isoformat() if order.created_at else None,
                "expires_at": order.expires_at.isoformat() if order.expires_at else None,
            }
        )
    return out


def cancel_all_orders(client: SecureClient) -> dict:
    """Cancel every resting order. WRITE OPERATION.

    Cancelling never spends money, but it does remove take-profit orders that
    were working while the bot was offline - say so before calling it.
    """
    try:
        response = client.cancel_all()
    except Exception as exc:
        return {
            "ok": False,
            "canceled": [],
            "canceled_count": 0,
            "not_canceled": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    canceled = [str(order_id) for order_id in response.canceled]
    not_canceled = {str(k): str(v) for k, v in (response.not_canceled or {}).items()}
    return {
        "ok": not not_canceled,
        "canceled": canceled,
        "canceled_count": len(canceled),
        "not_canceled": not_canceled,
        "error": None,
    }
