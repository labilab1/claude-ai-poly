"""Educational, read-only analysis layer — the "advisor".

Design principle: this advisor does NOT predict winners or tell you a trade
will make money. Nobody can do that reliably, and it's your capital at risk.
What it *does* do is turn raw market data into plain-English structure so you
can learn to read a prediction market yourself:

  - what the price is actually saying (implied probability),
  - how liquid / tradable the market is (spread + depth),
  - how much time is left until it resolves,
  - whether it pays liquidity-provider rewards.

Every briefing ends with an explicit reminder that this is analysis, not
financial advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from polymarket import Market

from polymarket_bot.markets import BookSnapshot, daily_reward_rate, is_tradable

DISCLAIMER = (
    "This is educational analysis of public market data, not financial advice "
    "or a prediction. Prediction markets are speculative and you can lose your "
    "entire stake. Only risk money you can afford to lose, and make your own "
    "decisions."
)

# Below this many shares near the touch, a tight spread is decoration: the
# first ordinary-sized order eats the level and prices its own slippage.
_SHALLOW_SHARES = 200.0


def implied_probability_note(price: float) -> str:
    pct = round(price * 100)
    if price <= 0.05:
        conf = "the market sees this as very unlikely"
    elif price <= 0.25:
        conf = "the market sees this as unlikely"
    elif price <= 0.45:
        conf = "the market leans against this"
    elif price <= 0.55:
        conf = "the market sees this as roughly a coin flip"
    elif price <= 0.75:
        conf = "the market leans toward this"
    elif price <= 0.95:
        conf = "the market sees this as likely"
    else:
        conf = "the market sees this as near-certain"
    return f"~{pct}% implied - {conf}."


def liquidity_note(snapshot: BookSnapshot) -> tuple[str, str]:
    """Return (label, explanation) describing how tradable the book is.

    Depth here is what sits *near the touch*, not the whole book. A book can
    show 80,000 resting shares and still be untradable if 60,000 of them are
    parked at $0.01, so quoting the total would be false comfort — and a tight
    spread over a handful of shares is not liquidity either, which is why a
    shallow book overrides the spread-based label.
    """
    spread = snapshot.spread
    if spread is None:
        return ("unknown", "One side of the book is empty, so there's no usable price yet.")
    cents = spread * 100
    near = (snapshot.bid_depth or 0.0) + (snapshot.ask_depth or 0.0)
    total = (snapshot.total_bid_depth or 0.0) + (snapshot.total_ask_depth or 0.0)
    window = (snapshot.depth_tolerance or 0.0) * 100

    if cents <= 1:
        label, verdict = "tight / liquid", "easy to get in and out near a fair price"
    elif cents <= 3:
        label, verdict = "moderate", "tradable, but friction adds up on round trips"
    else:
        label, verdict = "wide / thin", "thin book; you'll pay a lot to cross it"

    if near < _SHALLOW_SHARES:
        # A tight quote over a handful of shares is not liquidity. Replace the
        # spread verdict rather than appending to it: "easy to get in and out,
        # but shallow" is exactly the false comfort this note exists to avoid.
        label = "thin at the touch"
        verdict = "but there are barely any shares behind that quote"

    note = f"Spread is ~{cents:.1f}c - {verdict}."
    if window > 0:
        note += f" About {near:,.0f} shares rest within {window:.0f}c of the touch"
        # `total` is 0 on a snapshot built without the depth fields; don't
        # invent a "(of 0 in the book)" that reads like an empty book.
        note += f" (of {total:,.0f} in the whole book)." if total > near else "."
    else:
        note += f" About {near:,.0f} shares resting."
    if label == "thin at the touch":
        note += " Anything larger walks the book and fills well away from the quote."
    return (label, note)


def time_to_resolution(end_date: datetime | None) -> tuple[int | None, str]:
    if end_date is None:
        return (None, "No end date published.")
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    days = (end_date - datetime.now(timezone.utc)).days
    if days < 0:
        return (days, "End date has passed - may be resolving or already closed.")
    if days == 0:
        return (days, "Resolves within a day - very short horizon.")
    if days <= 7:
        return (days, f"~{days} day(s) left - short horizon, news moves price fast.")
    if days <= 60:
        return (days, f"~{days} days left - medium horizon.")
    return (days, f"~{days} days left - long horizon; capital is tied up a while.")


@dataclass
class Briefing:
    """One market explained in plain English. Always carries the disclaimer.

    `to_dict` includes `disclaimer` on purpose: a Telegram layer that
    serializes a briefing must not be able to drop it by accident.
    """

    question: str
    slug: str
    lines: list[str]

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "slug": self.slug,
            "lines": list(self.lines),
            "disclaimer": DISCLAIMER,
        }

    def to_text(self) -> str:
        # ASCII only: this string reaches a Windows console on the cp1255 code
        # page, where a typographic bullet is a UnicodeEncodeError.
        header = f"* {self.question}"
        body = "\n".join(f"  {ln}" for ln in self.lines)
        return f"{header}\n{body}\n  ! {DISCLAIMER}"


def briefing(market: Market, snapshot: BookSnapshot | None = None) -> Briefing:
    """Build an educational briefing for one market."""
    lines: list[str] = []

    yes, no = market.outcomes.yes, market.outcomes.no
    if yes.price is not None:
        lines.append(f"Outcome '{yes.label}': {implied_probability_note(float(yes.price))}")
    if no.price is not None:
        lines.append(f"Opposite '{no.label}': ~{round(float(no.price) * 100)}% implied.")

    days, time_note = time_to_resolution(market.state.end_date)
    lines.append(f"Time: {time_note}")

    if snapshot is not None:
        label, note = liquidity_note(snapshot)
        lines.append(f"Liquidity ({label}): {note}")
        if snapshot.best_bid is not None and snapshot.best_ask is not None:
            lines.append(
                f"Book: best buy {snapshot.best_bid:.3f} / best sell {snapshot.best_ask:.3f}."
            )

    reward = daily_reward_rate(market)
    if reward > 0:
        min_size = market.rewards.rewards_min_size
        lines.append(
            f"Rewards: pays ~{reward:g}/day to liquidity providers "
            f"who quote near the midpoint (min size {min_size if min_size is not None else '?'})."
        )

    if not is_tradable(market):
        lines.append("NOTE: this market is not currently accepting orders.")

    return Briefing(question=market.question or market.slug or "", slug=market.slug or "", lines=lines)
