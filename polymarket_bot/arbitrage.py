"""Mechanical arbitrage detection. Read-only, and deliberately narrow.

This is the one edge in the system that needs no forecast. Every other module
refuses to predict which outcome wins, and this one does not predict either -
it looks for prices that are inconsistent *with each other*, which is an
arithmetic fact rather than a view.

THE ONLY PATTERN IMPLEMENTED
----------------------------
A binary market's YES and NO tokens together always redeem for exactly $1.00:
one of them pays $1 and the other pays $0, whichever way it resolves. So if you
can BUY one of each for less than $1.00 combined, the difference is locked in
the moment both legs fill, regardless of the outcome.

    cost = ask(YES) + ask(NO)      edge = 1.00 - cost

Everything here hangs on `ask` meaning *the price you would actually pay*. The
gamma price fields are last/mid quotes and are useless for this: an edge
computed from a midpoint is an edge that does not exist. So candidates are
priced by walking the real order book, and a market whose book cannot be read
is skipped rather than guessed at.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not trade. `find_arbitrage` returns a report; there is no execution
path in this module and no caller wires one. Two reasons, both real:

  * **Leg risk.** The profit exists only if BOTH legs fill. Polymarket has no
    atomic two-leg order, so filling one and missing the other converts a
    "risk-free" trade into a naked directional position at a price nobody
    chose.
  * **Fees.** Taker fees reach 5% on some markets - larger than essentially
    every edge this finder can detect. `net_edge` subtracts a fee estimate, and
    an opportunity whose edge does not survive it is reported as not being one.

It also does not attempt multi-outcome (neg-risk) baskets. The arithmetic is
sound - N mutually exclusive outcomes must sum to $1 - but it needs every leg
of the basket to be readable and fillable simultaneously, and the leg risk
multiplies by N. That is a separate design decision, not an oversight.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from polymarket import SecureClient

from polymarket_bot.markets import is_tradable, market_url

#: A pair redeems for exactly this much, always.
PAIR_PAYOUT = 1.0

#: Below this the "edge" is inside the noise of tick rounding and a moving
#: book. Reporting a 0.1c edge as an opportunity is how you lose money paying
#: fees to collect nothing.
MIN_EDGE = 0.005  # half a cent per pair

#: Assumed taker fee when the market does not tell us otherwise. Polymarket
#: charges takers on many markets and 5% exists in the wild; assuming zero
#: would make every marginal edge look real.
DEFAULT_TAKER_FEE = 0.02

#: Ignore book levels smaller than this - a 1-share quote is not liquidity, and
#: sizing an opportunity off one is how the dust-top-of-book bug worked.
MIN_LEVEL_SHARES = 5.0


@dataclass
class ArbOpportunity:
    """One market whose YES and NO asks sum to less than the $1 they redeem for."""

    condition_id: str
    slug: str
    question: str
    url: str | None
    yes_price: float
    no_price: float
    #: Pairs available at the quoted prices, limited by the thinner side.
    size_pairs: float
    fee_rate: float
    warnings: list[str] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return round(self.yes_price + self.no_price, 6)

    @property
    def gross_edge(self) -> float:
        """Profit per pair before fees."""
        return round(PAIR_PAYOUT - self.cost, 6)

    @property
    def fee_cost(self) -> float:
        """Fees are charged on what you spend, on both legs."""
        return round(self.cost * self.fee_rate, 6)

    @property
    def net_edge(self) -> float:
        return round(self.gross_edge - self.fee_cost, 6)

    @property
    def gross_profit(self) -> float:
        return round(self.gross_edge * self.size_pairs, 6)

    @property
    def net_profit(self) -> float:
        return round(self.net_edge * self.size_pairs, 6)

    @property
    def edge_pct(self) -> float:
        return round((self.gross_edge / self.cost) * 100, 3) if self.cost else 0.0

    @property
    def survives_fees(self) -> bool:
        return self.net_edge > 0

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "slug": self.slug,
            "question": self.question,
            "url": self.url,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "cost": self.cost,
            "gross_edge": self.gross_edge,
            "net_edge": self.net_edge,
            "edge_pct": self.edge_pct,
            "size_pairs": self.size_pairs,
            "gross_profit": self.gross_profit,
            "net_profit": self.net_profit,
            "fee_rate": self.fee_rate,
            "survives_fees": self.survives_fees,
            "warnings": list(self.warnings),
        }


def best_ask(book: object) -> tuple[float | None, float]:
    """(price, shares) of the cheapest meaningful ask, or (None, 0).

    Polymarket sorts asks DESCENDING, so the best (lowest) ask is the LAST
    element - the same trap documented in the project notes. This reads by
    price rather than by position so it cannot be broken by a sort change.

    Levels below `MIN_LEVEL_SHARES` are ignored: a one-share quote is not a
    price you can trade a pair against, and treating it as one manufactures
    opportunities that vanish on contact.
    """
    levels = getattr(book, "asks", None) or ()
    usable = []
    for level in levels:
        try:
            price = float(level.price)
            size = float(level.size)
        except (TypeError, ValueError, AttributeError):
            continue
        if size >= MIN_LEVEL_SHARES and 0.0 < price <= 1.0:
            usable.append((price, size))
    if not usable:
        return (None, 0.0)
    return min(usable, key=lambda pair: pair[0])


def evaluate_market(
    client: SecureClient, market: object, *, fee_rate: float = DEFAULT_TAKER_FEE
) -> ArbOpportunity | None:
    """Price one market's YES/NO pair from live books.

    Returns None when there is no edge, or when the market cannot be priced -
    an unreadable book is not an opportunity, and must never be treated as a
    free one.
    """
    yes = market.outcomes.yes
    no = market.outcomes.no
    if not (yes.token_id and no.token_id):
        return None
    if not is_tradable(market):
        return None

    try:
        yes_book = client.get_order_book(token_id=str(yes.token_id))
        no_book = client.get_order_book(token_id=str(no.token_id))
    except Exception:
        return None

    yes_price, yes_size = best_ask(yes_book)
    no_price, no_size = best_ask(no_book)
    if yes_price is None or no_price is None:
        return None

    cost = yes_price + no_price
    if PAIR_PAYOUT - cost < MIN_EDGE:
        return None

    warnings: list[str] = []
    size = min(yes_size, no_size)
    if size < MIN_LEVEL_SHARES:
        return None

    min_order = float(getattr(getattr(market, "trading", None), "minimum_order_size", 0) or 0)
    if min_order and size < min_order:
        warnings.append(
            f"Only {size:,.0f} pairs available but the exchange minimum is "
            f"{min_order:,.0f} shares - this may not be placeable."
        )
    if yes_size != no_size:
        warnings.append(
            f"Legs are uneven ({yes_size:,.0f} YES vs {no_size:,.0f} NO at these prices); "
            f"the smaller side is what caps the trade."
        )

    return ArbOpportunity(
        condition_id=str(getattr(market, "condition_id", "") or ""),
        slug=str(getattr(market, "slug", "") or ""),
        question=str(getattr(market, "question", "") or getattr(market, "slug", "") or ""),
        url=market_url(market),
        yes_price=round(yes_price, 6),
        no_price=round(no_price, 6),
        size_pairs=round(size, 2),
        fee_rate=fee_rate,
        warnings=warnings,
    )


def _prefilter(market: object) -> bool:
    """Cheap gamma-price screen before paying for two order-book reads.

    Gamma prices are not executable, so this cannot decide anything - it only
    decides what is worth *looking at*. The window is deliberately generous
    (real asks sit above the mid, so a pair that looks slightly over $1 here can
    still be under it on the book... and vice versa, which is why the book is
    what actually decides).
    """
    try:
        yes = float(market.outcomes.yes.price)
        no = float(market.outcomes.no.price)
    except (TypeError, ValueError, AttributeError):
        # No usable quote: let the book decide rather than filtering it out.
        return True
    return (yes + no) <= 1.04


def find_arbitrage(
    client: SecureClient,
    markets: list,
    *,
    fee_rate: float = DEFAULT_TAKER_FEE,
    max_books: int = 40,
) -> tuple[list[ArbOpportunity], int]:
    """Scan `markets` for YES+NO < $1. Returns (opportunities, markets_priced).

    `max_books` bounds the work: each candidate costs two order-book reads, and
    an unbounded scan over every live market is minutes of network I/O for a
    chat command. The count returned is how many were actually *priced*, not
    how many were passed in - a report that says "scanned 500" after pricing 40
    is a lie about how hard it looked.
    """
    found: list[ArbOpportunity] = []
    priced = 0
    for market in itertools.islice((m for m in markets if _prefilter(m)), max_books):
        priced += 1
        opportunity = evaluate_market(client, market, fee_rate=fee_rate)
        if opportunity is not None:
            found.append(opportunity)
    found.sort(key=lambda o: o.net_edge, reverse=True)
    return (found, priced)
