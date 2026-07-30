"""Read-only market data access for Polymarket.

Everything here is read-only: fetching markets, order books, prices and
spreads. Nothing in this module can place, cancel, or modify an order.

Price semantics: on Polymarket each outcome token trades between 0 and 1, and
its price is the market's implied probability that the outcome resolves YES
(a share pays out $1 if it wins, $0 if it loses). A "Yes" price of 0.62 means
the market prices that outcome at ~62%.

Built on the official `polymarket-client` SDK (import name `polymarket`),
which replaced the now-archived `py-clob-client`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from polymarket import Market, OrderBook, SecureClient

# How far from the touch a resting order still counts as liquidity you can
# reach. Summing the *whole* book is close to meaningless: a book routinely
# carries tens of thousands of shares parked at $0.01 that no realistic order
# ever touches, which makes every "is this book deep enough?" test pass. Two
# cents either side of the touch is roughly how far a market order can walk
# before the fill stops resembling the quoted price.
_DEPTH_TOLERANCE = 0.02

# Slack on the depth window's edge, well under one tick: prices are 4dp at most.
_EDGE = 1e-9


def is_tradable(market: Market) -> bool:
    s = market.state
    return bool(s.active and not s.closed and s.accepting_orders)


def daily_reward_rate(market: Market) -> float:
    rewards = market.rewards.clob_rewards or ()
    return float(sum(r.rewards_daily_rate for r in rewards))


def _depth(
    levels: Iterable[Any], *, floor: float | None = None, ceiling: float | None = None
) -> tuple[float, float]:
    """(shares, notional_usdc) over the levels priced inside [floor, ceiling].

    Filtering by price rather than by list position keeps this correct no
    matter which way round the venue sorts a side.
    """
    shares = 0.0
    notional = 0.0
    for level in levels:
        price = float(level.price)
        if floor is not None and price < floor:
            continue
        if ceiling is not None and price > ceiling:
            continue
        size = float(level.size)
        shares += size
        notional += size * price
    return (shares, notional)


@dataclass
class BookSnapshot:
    """The top of one token's book, plus how much of it is actually reachable.

    `bid_depth` / `ask_depth` are the shares resting **within
    `depth_tolerance` of the touch** — the liquidity a market order really
    sweeps, and the only depth number worth comparing an order size against.
    They are deliberately *not* the whole book: `total_bid_depth` /
    `total_ask_depth` are, and on a book with a parked $0.01 wall the two
    differ by an order of magnitude.

    `bid_notional` is the USDC a SELL would realise into `bid_depth`;
    `ask_notional` is the USDC a BUY needs to lift `ask_depth`.
    """

    token_id: str
    best_bid: float | None
    best_ask: float | None
    midpoint: float | None
    bid_depth: float
    ask_depth: float
    # Appended rather than interleaved, and all defaulted, so any existing
    # positional or partial-keyword construction keeps working unchanged.
    total_bid_depth: float = 0.0
    total_ask_depth: float = 0.0
    bid_notional: float = 0.0
    ask_notional: float = 0.0
    depth_tolerance: float = _DEPTH_TOLERANCE

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 4)

    @classmethod
    def from_book(
        cls, token_id: str, book: OrderBook, *, tolerance: float = _DEPTH_TOLERANCE
    ) -> BookSnapshot:
        """Build a snapshot from a raw `OrderBook` (bids ASC, asks DESC)."""
        bids = tuple(book.bids or ())
        asks = tuple(book.asks or ())
        best_bid = float(bids[-1].price) if bids else None
        best_ask = float(asks[-1].price) if asks else None

        # _EDGE widens the window by a hair: `0.61 - 0.02` can land a whisker
        # above 0.59 in binary floating point, which would silently drop a
        # whole level that sits exactly on the boundary.
        near_bid_shares, near_bid_notional = (
            _depth(bids, floor=best_bid - tolerance - _EDGE) if best_bid is not None else (0.0, 0.0)
        )
        near_ask_shares, near_ask_notional = (
            _depth(asks, ceiling=best_ask + tolerance + _EDGE) if best_ask is not None else (0.0, 0.0)
        )
        total_bid_shares, _ = _depth(bids)
        total_ask_shares, _ = _depth(asks)

        return cls(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=round((best_bid + best_ask) / 2, 4)
            if best_bid is not None and best_ask is not None
            else None,
            bid_depth=round(near_bid_shares, 2),
            ask_depth=round(near_ask_shares, 2),
            total_bid_depth=round(total_bid_shares, 2),
            total_ask_depth=round(total_ask_shares, 2),
            bid_notional=round(near_bid_notional, 4),
            ask_notional=round(near_ask_notional, 4),
            depth_tolerance=tolerance,
        )

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "midpoint": self.midpoint,
            "spread": self.spread,
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "total_bid_depth": self.total_bid_depth,
            "total_ask_depth": self.total_ask_depth,
            "bid_notional": self.bid_notional,
            "ask_notional": self.ask_notional,
            "depth_tolerance": self.depth_tolerance,
        }


def list_tradable_markets(
    client: SecureClient,
    limit: int = 50,
    *,
    order: str | None = None,
    ascending: bool | None = None,
) -> list[Market]:
    """Return up to `limit` currently-tradable markets (live, accepting orders).

    `order` is passed to the API when given. `order="volume24hr"` with
    `ascending=False` is a genuine hot-first ordering; `volumeNum` and
    `liquidityNum` sort by fields unrelated to recent activity and are not
    worth offering. Omitting both keeps the API's default ordering, which is
    what every existing caller expects - so they are only sent when asked for,
    rather than sent as None.

    The `break` is load-bearing: `iter_items()` pages through the whole result
    set, and walking it unbounded against `closed=False` hangs the process.
    """
    query: dict[str, object] = {"closed": False, "page_size": min(limit, 100)}
    if order is not None:
        query["order"] = order
    if ascending is not None:
        query["ascending"] = ascending

    out: list[Market] = []
    for market in client.list_markets(**query).iter_items():
        if is_tradable(market) and market.outcomes.yes.token_id:
            out.append(market)
            if len(out) >= limit:
                break
    return out


def market_url(market: Market) -> str | None:
    """A link to this market on polymarket.com, or None if one cannot be built.

    Canonical form is `/event/<event-slug>/<market-slug>`; `/market/<slug>` is
    also a real route that redirects to it, and is the fallback for a market
    that carries no event. Both verified live.

    Returns None rather than a half-built path when the slug is missing: a
    link to `/market/` looks like a working button and lands on a 404.
    """
    try:
        slug = (getattr(market, "slug", None) or "").strip()
        if not slug:
            return None
        events = getattr(market, "events", None) or ()
        event_slug = ""
        if events:
            event_slug = (getattr(events[0], "slug", None) or "").strip()
        if event_slug:
            return f"https://polymarket.com/event/{event_slug}/{slug}"
        return f"https://polymarket.com/market/{slug}"
    except Exception:
        # Market models change shape between SDK versions; that should cost a
        # link, not the screen the link was going to sit on.
        return None


def get_market_by_condition_id(client: SecureClient, condition_id: str) -> Market:
    markets = list(client.list_markets(condition_ids=condition_id).iter_items())
    if not markets:
        raise ValueError(f"No market found for condition_id {condition_id}")
    return markets[0]


def get_market_by_slug(client: SecureClient, slug: str) -> Market:
    return client.get_market(slug=slug)


def get_book_snapshot(
    client: SecureClient, token_id: str, *, depth_tolerance: float = _DEPTH_TOLERANCE
) -> BookSnapshot:
    """Top of book plus reachable depth. See `BookSnapshot` for what depth means."""
    book = client.get_order_book(token_id=token_id)
    return BookSnapshot.from_book(token_id, book, tolerance=depth_tolerance)


def get_spreads(client: SecureClient, token_ids: list[str]) -> dict[str, float]:
    """Batch-fetch spreads for many tokens. Returns {token_id: spread}."""
    if not token_ids:
        return {}
    raw = client.get_spreads(token_ids=token_ids)
    return {tid: float(raw[tid]) for tid in token_ids if tid in raw}
