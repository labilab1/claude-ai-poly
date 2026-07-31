"""Everything worth knowing about one market, gathered in one pass.

This is the deep screen. The rest of the bot is deliberately terse - a list, a
balance, a confirmation - but when someone opens a market they want the whole
picture, so this collects it: what the price implies, what a round trip costs,
what the book can actually absorb, where the price has been, and what a
position would look like against the account's own limits.

WHAT THIS IS NOT
----------------
It does not say which side will win, and it never will. Nothing in this
project forecasts outcomes (see `advisor.py`), and a "recommendation" derived
from a spread and a volume number would be a guess wearing a suit.

What it CAN say honestly is mechanical, and most of it is more decision-useful
than a prediction would be:

  * the price IS the market's probability estimate - stated plainly,
  * the round trip costs a knowable number of cents, so a position has to move
    by at least that much before it is worth anything,
  * the break-even price, which is the number people actually need and rarely
    compute,
  * how many shares the book can absorb before the fill stops resembling the
    quote,
  * whether the account can even take a position of a given size under its own
    caps.

A market can be liquid, cheap to trade, heavily traded, and still resolve
against you. Every field here is a fact about the market's structure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from polymarket import SecureClient

from polymarket_bot.config import Settings
from polymarket_bot.markets import BookSnapshot, daily_reward_rate, get_book_snapshot, is_tradable

#: Unicode blocks, low to high. A price line drawn in text.
_SPARK = "▁▂▃▄▅▆▇█"

#: History window for the sparkline.
_HISTORY_INTERVAL = "1d"
_HISTORY_FIDELITY = 60


def sparkline(values: list[float], width: int = 24) -> str:
    """A tiny price chart in text.

    Scaled to the series' own min/max rather than to 0-1: a market that spent
    the day between 0.60 and 0.64 would otherwise render as a flat line and
    say nothing. The caller prints the real range alongside, so the relative
    shape here is not misleading.
    """
    points = [v for v in values if isinstance(v, (int, float))]
    if len(points) < 2:
        return ""
    # Downsample by averaging buckets, so a 168-point week still fits.
    if len(points) > width:
        bucket = len(points) / width
        points = [
            sum(points[int(i * bucket) : max(int((i + 1) * bucket), int(i * bucket) + 1)])
            / max(len(points[int(i * bucket) : max(int((i + 1) * bucket), int(i * bucket) + 1)]), 1)
            for i in range(width)
        ]
    low, high = min(points), max(points)
    if high - low < 1e-9:
        return _SPARK[0] * len(points)
    span = high - low
    return "".join(_SPARK[min(int((v - low) / span * (len(_SPARK) - 1)), len(_SPARK) - 1)] for v in points)


@dataclass
class SideCost:
    """What one side costs to get into and out of, right now."""

    label: str
    outcome: str  # "yes" / "no"
    price: float | None  # the ask - what a buy pays
    exit_price: float | None  # the bid - what a sell fetches
    depth_shares: float | None

    @property
    def round_trip(self) -> float | None:
        """Cents given up buying at the ask and selling at the bid."""
        if self.price is None or self.exit_price is None:
            return None
        return round(self.price - self.exit_price, 6)

    @property
    def break_even(self) -> float | None:
        """Where the price must get to before an exit clears the spread."""
        return round(self.price, 6) if self.price is not None else None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "outcome": self.outcome,
            "price": self.price,
            "exit_price": self.exit_price,
            "depth_shares": self.depth_shares,
            "round_trip": self.round_trip,
            "break_even": self.break_even,
        }


@dataclass
class MarketAnalysis:
    """The full picture for one market. JSON-safe throughout."""

    question: str
    slug: str
    condition_id: str
    url: str | None
    tradable: bool
    days_left: int | None
    end_date: str | None
    volume_24h: float | None
    liquidity: float | None
    daily_reward: float
    yes: SideCost
    no: SideCost
    spread: float | None
    pair_cost: float | None  # yes ask + no ask; 100c is fair
    history: list[float] = field(default_factory=list)
    history_change: float | None = None
    #: Largest position the account's own limits allow.
    max_order_usdc: float = 0.0
    affordable_usdc: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def chart(self) -> str:
        return sparkline(self.history)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "slug": self.slug,
            "condition_id": self.condition_id,
            "url": self.url,
            "tradable": self.tradable,
            "days_left": self.days_left,
            "end_date": self.end_date,
            "volume_24h": self.volume_24h,
            "liquidity": self.liquidity,
            "daily_reward": self.daily_reward,
            "yes": self.yes.to_dict(),
            "no": self.no.to_dict(),
            "spread": self.spread,
            "pair_cost": self.pair_cost,
            "history": list(self.history),
            "history_change": self.history_change,
            "chart": self.chart,
            "max_order_usdc": self.max_order_usdc,
            "affordable_usdc": self.affordable_usdc,
            "notes": list(self.notes),
        }


def _side(label: str, outcome: str, snapshot: BookSnapshot | None) -> SideCost:
    if snapshot is None:
        return SideCost(label=label, outcome=outcome, price=None, exit_price=None, depth_shares=None)
    return SideCost(
        label=label,
        outcome=outcome,
        price=snapshot.best_ask,
        exit_price=snapshot.best_bid,
        depth_shares=snapshot.ask_depth,
    )


def _history(client: SecureClient, token_id: str) -> tuple[list[float], float | None]:
    """Recent prices, newest last. Empty on any failure - a missing chart is
    not worth failing the screen for."""
    try:
        points = client.get_price_history(
            token_id=token_id, interval=_HISTORY_INTERVAL, fidelity=_HISTORY_FIDELITY
        )
    except Exception:
        return ([], None)
    values: list[float] = []
    for point in points:
        # The model exposes t/p; go through model_dump so a field rename is a
        # missing chart rather than an exception.
        try:
            data = point.model_dump()
            price = float(data.get("p"))
        except Exception:
            continue
        if 0.0 < price <= 1.0:
            values.append(price)
    change = round(values[-1] - values[0], 6) if len(values) >= 2 else None
    return (values, change)


def analyse(
    client: SecureClient,
    market: object,
    settings: Settings,
    *,
    cash_usdc: float | None = None,
) -> MarketAnalysis:
    """Gather everything about `market`. Read-only; places nothing."""
    from polymarket_bot.advisor import time_to_resolution
    from polymarket_bot.markets import market_url

    yes, no = market.outcomes.yes, market.outcomes.no
    notes: list[str] = []

    yes_book = no_book = None
    if yes.token_id:
        try:
            yes_book = get_book_snapshot(client, str(yes.token_id))
        except Exception as exc:
            notes.append(f"YES book unavailable ({type(exc).__name__}).")
    if no.token_id:
        try:
            no_book = get_book_snapshot(client, str(no.token_id))
        except Exception as exc:
            notes.append(f"NO book unavailable ({type(exc).__name__}).")

    yes_side = _side(str(yes.label or "Yes"), "yes", yes_book)
    no_side = _side(str(no.label or "No"), "no", no_book)

    history, change = ([], None)
    if yes.token_id:
        history, change = _history(client, str(yes.token_id))

    pair_cost = None
    if yes_side.price is not None and no_side.price is not None:
        pair_cost = round(yes_side.price + no_side.price, 6)

    days, _note = time_to_resolution(market.state.end_date)

    affordable = None
    if cash_usdc is not None:
        # The smaller of what is in the account and what the per-order cap
        # allows - the number that actually bounds a position here.
        affordable = round(min(float(cash_usdc), float(settings.max_order_usdc)), 2)

    if not is_tradable(market):
        notes.append("This market is not accepting orders right now.")

    return MarketAnalysis(
        question=str(market.question or market.slug or ""),
        slug=str(market.slug or ""),
        condition_id=str(market.condition_id or ""),
        url=market_url(market),
        tradable=is_tradable(market),
        days_left=days,
        end_date=market.state.end_date.isoformat() if market.state.end_date else None,
        volume_24h=float(market.metrics.volume_24hr) if market.metrics.volume_24hr else None,
        liquidity=float(market.metrics.liquidity_num) if market.metrics.liquidity_num else None,
        daily_reward=daily_reward_rate(market),
        yes=yes_side,
        no=no_side,
        spread=yes_book.spread if yes_book else None,
        pair_cost=pair_cost,
        history=history,
        history_change=change,
        max_order_usdc=float(settings.max_order_usdc),
        affordable_usdc=affordable,
        notes=notes,
    )
