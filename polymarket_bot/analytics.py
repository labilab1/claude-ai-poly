"""Read-only analytics: what the account actually did, and what is tradable now.

Two jobs, kept strictly separate:

1. **History** (`get_trade_stats`, `get_insights`) — reconstructs the account's
   own record from Polymarket's own numbers and reports it straight, wins and
   losses alike. Every stat says what it was computed from.
2. **Screening** (`find_opportunities`) — ranks live markets on *tradability and
   structure only*: spread, resting liquidity, real volume, maker-reward rate,
   time to resolution, and whether the price is pinned against 0/1. It does not
   and cannot say which outcome will win; a high score means "cheap to get in
   and out of", never "likely to pay".

Nothing here places, cancels or redeems anything.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import islice
from typing import Any, Iterable, Literal, Sequence

from polymarket import Market, SecureClient

from polymarket_bot.advisor import time_to_resolution
from polymarket_bot.config import Settings
from polymarket_bot.markets import daily_reward_rate, get_spreads, list_tradable_markets

Severity = Literal["info", "warn", "good"]

# Titles/slugs of the 5-minute crypto "Up or Down" series. Matched on both
# because the Gamma title and the CLOB slug use different conventions.
_SHORT_HORIZON_TITLE = re.compile(r"up\s*or\s*down", re.IGNORECASE)
_SHORT_HORIZON_SLUG = re.compile(r"updown-\d+m", re.IGNORECASE)

_EPS = 1e-9
_PRICE_EPS = 1e-6
_PAGE = 50  # data-api caps page_size at 50
_SPREAD_CHUNK = 50  # tokens per get_spreads call


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _f(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


def _money(value: Decimal | float | int | None) -> float:
    return 0.0 if value is None else round(float(value), 4)


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _as_utc(value: datetime | None) -> datetime | None:
    """Both trade feeds should return aware timestamps; don't trust it blindly.

    A naive datetime mixed into the same sort/min/max as an aware one raises.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_short_horizon(title: str | None, slug: str | None) -> bool:
    return bool(
        (title and _SHORT_HORIZON_TITLE.search(title))
        or (slug and _SHORT_HORIZON_SLUG.search(slug))
    )


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


# --------------------------------------------------------------------------
# public dataclasses
# --------------------------------------------------------------------------
@dataclass
class TradeStats:
    """Record of *completed* positions (closed out, or resolved and final).

    `total_trades` counts completed positions, not order fills — a position is
    the unit that has a profit or loss. `win_rate` is a fraction in 0..1 over
    wins+losses (break-even results are excluded from the ratio but still
    counted in `total_trades`). `total_pnl` is realized only; open positions
    that have not resolved are excluded so this number cannot move on price.
    """

    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    best: dict | None
    worst: dict | None

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "total_pnl": self.total_pnl,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "best": dict(self.best) if self.best else None,
            "worst": dict(self.worst) if self.worst else None,
        }


@dataclass
class Insight:
    headline: str
    detail: str
    severity: Severity = "info"
    #: Stable identifier for this kind of insight, plus the numbers that went
    #: into its headline. A front end that speaks another language renders
    #: `code` + `params` itself rather than trying to translate the English
    #: prose above - the CLI keeps `headline`/`detail` exactly as they are.
    code: str = ""
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "headline": self.headline,
            "detail": self.detail,
            "severity": self.severity,
            "code": self.code,
            "params": dict(self.params),
        }


@dataclass
class Opportunity:
    """A market that is *structurally* easy to trade. Not a prediction."""

    condition_id: str
    token_id: str
    market_title: str
    slug: str | None
    outcome: str
    price: float
    spread: float | None
    liquidity: float | None
    volume_24h: float | None
    daily_reward: float
    days_left: int | None
    score: float
    reasons: list[str]

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "market_title": self.market_title,
            "slug": self.slug,
            "outcome": self.outcome,
            "price": self.price,
            "spread": self.spread,
            "liquidity": self.liquidity,
            "volume_24h": self.volume_24h,
            "daily_reward": self.daily_reward,
            "days_left": self.days_left,
            "score": self.score,
            "reasons": list(self.reasons),
        }


# --------------------------------------------------------------------------
# internal history model
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _Fill:
    """One executed fill, normalised to *our* side of the trade."""

    tx: str
    condition_id: str
    token_id: str | None
    side: str  # BUY / SELL, from our perspective
    size: float
    price: float
    outcome: str | None
    title: str | None
    slug: str | None
    timestamp: datetime | None
    trader_side: str | None
    fee_rate_bps: float | None

    @property
    def notional(self) -> float:
        return self.size * self.price


@dataclass(frozen=True)
class _Result:
    """P&L outcome for one (market, outcome token) the account traded."""

    condition_id: str
    token_id: str | None
    title: str
    slug: str | None
    outcome: str | None
    pnl: float
    cost_basis: float
    settled: bool  # closed out, or resolved to 0/1 — i.e. the P&L is final
    redeemable: bool
    source: str  # "closed" | "position" | "fills"
    end_date: str | None

    @property
    def short_horizon(self) -> bool:
        return _is_short_horizon(self.title, self.slug)

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "market": self.title,
            "slug": self.slug,
            "outcome": self.outcome,
            "pnl": round(self.pnl, 4),
            "cost_basis": round(self.cost_basis, 4),
            "settled": self.settled,
            "source": self.source,
            "end_date": self.end_date,
        }


def _from_clob_trade(trade: Any) -> _Fill:
    """Normalise a ClobTrade to the account's own side.

    A trade we *made* (trader_side == "MAKER") is reported from the taker's
    point of view: `.side`/`.price`/`.size` describe the counterparty, and our
    leg sits in `.maker_orders`. Taking `.side` at face value there would flip
    every maker fill's direction.
    """
    side = str(trade.side)
    size = float(trade.size)
    price = float(trade.price)
    if str(getattr(trade, "trader_side", "") or "").upper() == "MAKER":
        legs = [m for m in (trade.maker_orders or ()) if m.owner == trade.owner]
        matched = sum(float(m.matched_amount) for m in legs)
        if legs and matched > 0:
            price = sum(float(m.price) * float(m.matched_amount) for m in legs) / matched
            size = matched
            side = str(legs[0].side)
        else:
            side = "SELL" if side == "BUY" else "BUY"
    matched_at = getattr(trade, "matched_at", None) or getattr(trade, "match_time", None)
    return _Fill(
        tx=str(trade.transaction_hash),
        condition_id=str(trade.condition_id),
        token_id=str(trade.token_id) if trade.token_id else None,
        side=side,
        size=size,
        price=price,
        outcome=trade.outcome,
        title=None,  # the CLOB view carries no market title
        slug=None,
        timestamp=_as_utc(matched_at),
        trader_side=getattr(trade, "trader_side", None),
        fee_rate_bps=_f(getattr(trade, "fee_rate_bps", None)),
    )


def _from_data_trade(trade: Any) -> _Fill | None:
    if not trade.transaction_hash or not trade.condition_id:
        return None
    return _Fill(
        tx=str(trade.transaction_hash),
        condition_id=str(trade.condition_id),
        token_id=str(trade.token_id) if trade.token_id else None,
        side=str(trade.side or "BUY"),
        size=float(trade.size or 0),
        price=float(trade.price or 0),
        outcome=trade.outcome,
        title=trade.title,
        slug=trade.slug,
        timestamp=_as_utc(trade.timestamp),
        trader_side=None,
        fee_rate_bps=None,
    )


def _load_fills(client: SecureClient, *, limit: int) -> list[_Fill]:
    """Merge both trade views into one deduped list of our fills.

    `list_account_trades` (CLOB) is authoritative for size/price/side but has
    no market title; `list_trades` (data-api) has the title. The same fill
    appears in both, and a CLOB fill can surface twice (maker and taker views),
    so everything is keyed by transaction hash.
    """
    fills: dict[str, _Fill] = {}
    for trade in islice(client.list_account_trades().iter_items(), limit):
        fill = _from_clob_trade(trade)
        if fill.tx not in fills:
            fills[fill.tx] = fill

    try:
        for trade in islice(client.list_trades(page_size=_PAGE).iter_items(), limit):
            extra = _from_data_trade(trade)
            if extra is None:
                continue
            known = fills.get(extra.tx)
            if known is None:
                fills[extra.tx] = extra
            elif known.title is None:
                fills[extra.tx] = replace(known, title=extra.title, slug=extra.slug)
    except Exception:  # title enrichment is a nicety; never fail the whole call
        pass

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(fills.values(), key=lambda f: f.timestamp or epoch, reverse=True)


def _is_resolved(cur_price: float | None, redeemable: bool | None) -> bool:
    if cur_price is None:
        return False
    # A resolved market prices its tokens at exactly 0 or 1; redeemable
    # confirms settlement rather than a market that merely trades near 1.
    return bool(redeemable) and (cur_price <= _PRICE_EPS or cur_price >= 1 - _PRICE_EPS)


def _round_trips(fills: Iterable[_Fill]) -> dict[tuple[str, str | None], _Result]:
    """Pair BUY/SELL fills per token to recover realized P&L from raw history.

    Only used for tokens Polymarket has no position row for; position rows are
    authoritative when they exist.
    """
    agg: dict[tuple[str, str | None], dict[str, Any]] = {}
    for fill in fills:
        key = (fill.condition_id, fill.token_id)
        acc = agg.setdefault(
            key,
            {
                "buy_shares": 0.0,
                "buy_cost": 0.0,
                "sell_shares": 0.0,
                "sell_proceeds": 0.0,
                "title": None,
                "slug": None,
                "outcome": fill.outcome,
            },
        )
        if fill.side.upper() == "BUY":
            acc["buy_shares"] += fill.size
            acc["buy_cost"] += fill.notional
        else:
            acc["sell_shares"] += fill.size
            acc["sell_proceeds"] += fill.notional
        acc["title"] = acc["title"] or fill.title
        acc["slug"] = acc["slug"] or fill.slug

    out: dict[tuple[str, str | None], _Result] = {}
    for key, acc in agg.items():
        if acc["buy_shares"] <= 0 or acc["sell_shares"] <= 0:
            continue  # never round-tripped: P&L is not realized yet
        avg_buy = acc["buy_cost"] / acc["buy_shares"]
        avg_sell = acc["sell_proceeds"] / acc["sell_shares"]
        matched = min(acc["buy_shares"], acc["sell_shares"])
        out[key] = _Result(
            condition_id=key[0],
            token_id=key[1],
            title=acc["title"] or key[0],
            slug=acc["slug"],
            outcome=acc["outcome"],
            pnl=matched * (avg_sell - avg_buy),
            cost_basis=matched * avg_buy,
            settled=True,
            redeemable=False,
            source="fills",
            end_date=None,
        )
    return out


def _collect_history(
    client: SecureClient, *, limit: int = 500
) -> tuple[list[_Result], list[_Fill]]:
    """Rebuild every P&L outcome the account has, one row per (market, token).

    Sources, in order of authority:
      1. open positions  — realized_pnl + cash_pnl (cash_pnl is the still-held
         size; on a resolved market it is the final win/loss)
      2. closed positions — realized_pnl for tokens fully exited
      3. paired fills     — only for tokens neither endpoint reports
    """
    fills = _load_fills(client, limit=limit)
    results: list[_Result] = []
    seen: set[tuple[str, str | None]] = set()

    for pos in islice(client.list_positions(size_threshold=0, page_size=_PAGE).iter_items(), limit):
        key = (str(pos.condition_id), str(pos.token_id) if pos.token_id else None)
        if key in seen:
            continue
        seen.add(key)
        cur = _f(pos.cur_price)
        resolved = _is_resolved(cur, pos.redeemable)
        results.append(
            _Result(
                condition_id=key[0],
                token_id=key[1],
                title=pos.title or key[0],
                slug=pos.slug,
                outcome=pos.outcome,
                # cash_pnl covers the held size, realized_pnl any earlier exits
                pnl=(_f(pos.cash_pnl) or 0.0) + (_f(pos.realized_pnl) or 0.0),
                cost_basis=_f(pos.initial_value) or 0.0,
                settled=resolved,
                redeemable=bool(pos.redeemable),
                source="position",
                end_date=_iso(pos.end_date),
            )
        )

    try:
        for pos in islice(
            client.list_closed_positions(page_size=_PAGE).iter_items(), limit
        ):
            key = (str(pos.condition_id), str(pos.token_id) if pos.token_id else None)
            if key in seen:
                continue
            seen.add(key)
            shares = _f(pos.total_bought) or 0.0
            results.append(
                _Result(
                    condition_id=key[0],
                    token_id=key[1],
                    title=pos.title or key[0],
                    slug=pos.slug,
                    outcome=pos.outcome,
                    pnl=_f(pos.realized_pnl) or 0.0,
                    cost_basis=shares * (_f(pos.avg_price) or 0.0),
                    settled=True,
                    redeemable=False,
                    source="closed",
                    end_date=_iso(pos.end_date),
                )
            )
    except Exception:
        # Older SDKs / API hiccups: fall through to fill pairing below.
        pass

    for key, result in _round_trips(fills).items():
        if key in seen:
            continue
        seen.add(key)
        results.append(result)

    return results, fills


def _stats_from(completed: list[_Result]) -> TradeStats:
    wins = [r for r in completed if r.pnl > _EPS]
    losses = [r for r in completed if r.pnl < -_EPS]
    decided = len(wins) + len(losses)
    best = max(completed, key=lambda r: r.pnl) if completed else None
    worst = min(completed, key=lambda r: r.pnl) if completed else None
    return TradeStats(
        total_trades=len(completed),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / decided, 4) if decided else 0.0,
        total_pnl=_money(sum(r.pnl for r in completed)),
        avg_win=_mean([r.pnl for r in wins]),
        avg_loss=_mean([r.pnl for r in losses]),
        best=best.to_dict() if best else None,
        worst=worst.to_dict() if worst else None,
    )


# --------------------------------------------------------------------------
# public API — history
# --------------------------------------------------------------------------
def get_trade_stats(client: SecureClient, *, limit: int = 500) -> TradeStats:
    """Win/loss record over completed positions. Realized numbers only."""
    results, _fills = _collect_history(client, limit=limit)
    return _stats_from([r for r in results if r.settled])


def _insight_overall(stats: TradeStats) -> Insight:
    flat = stats.total_trades - stats.wins - stats.losses
    ratio = abs(stats.avg_loss) / stats.avg_win if stats.avg_win > 0 else None
    detail = (
        f"{stats.wins} winners, {stats.losses} losers"
        + (f", {flat} break-even" if flat else "")
        + f" — a {stats.win_rate * 100:.0f}% hit rate. "
        f"Average winner {stats.avg_win:+.2f}, average loser {stats.avg_loss:+.2f}"
        + (
            f"; losers are {ratio:.1f}x the size of winners, which is why a near "
            "coin-flip hit rate still nets out negative."
            if ratio and ratio > 1.2
            else "."
        )
        + " Basis: Polymarket's own realized P&L per position (open positions "
        "that have not resolved are excluded), not an estimate."
    )
    return Insight(
        headline=f"{stats.total_trades} completed positions, net {stats.total_pnl:+.2f} USDC",
        detail=detail,
        severity="good" if stats.total_pnl > 0 else "warn",
        code="record",
        params={"count": stats.total_trades, "net": round(stats.total_pnl, 2)},
    )


def _insight_short_horizon(completed: list[_Result]) -> Insight | None:
    short = [r for r in completed if r.short_horizon]
    if len(short) < 3:
        return None
    share = len(short) / len(completed) * 100
    pnl = sum(r.pnl for r in short)
    rest = sum(r.pnl for r in completed if not r.short_horizon)
    return Insight(
        headline=(
            f"{len(short)} of {len(completed)} completed positions ({share:.0f}%) were "
            f"5-minute 'Up or Down' markets — net {pnl:+.2f} USDC"
        ),
        code="short_horizon",
        params={"count": len(short), "total": len(completed),
                "share": round(share), "net": round(pnl, 2)},
        detail=(
            "Those markets are structurally hostile to a taker, for reasons that have "
            "nothing to do with being right: (1) over a five-minute window the price is "
            "close to a coin flip, so there is no time for information to accumulate and "
            "no edge to find in the fundamentals; (2) you cross the spread going in and "
            "again coming out — a 2-3c spread on a ~50c contract is roughly a 5% round-trip "
            "cost, so you need well above a 55% hit rate just to break even; (3) fees, where "
            "charged, come off the same thin margin; (4) the resting quotes are largely "
            "automated and re-price faster than a manual order, so a taker order tends to "
            "fill exactly when the quote has already moved. "
            f"The rest of the book (non-short-horizon markets) came to {rest:+.2f} USDC."
        ),
        severity="warn",
    )


def _insight_unredeemed(results: list[_Result]) -> Insight | None:
    stuck = [r for r in results if r.source == "position" and r.settled and r.redeemable]
    if not stuck:
        return None
    lost = [r for r in stuck if r.pnl < -_EPS]
    basis = sum(r.cost_basis for r in stuck)
    if not lost:
        return Insight(
            headline=f"{len(stuck)} resolved positions are still unredeemed",
            detail=f"Redeem them to move ~{basis:.2f} USDC of settled value back to cash.",
            severity="info",
            code="unredeemed",
            params={"count": len(stuck), "amount": round(basis, 2)},
        )
    return Insight(
        headline=(
            f"{len(lost)} resolved positions are still sitting in the account, all at "
            f"about -100% ({sum(r.cost_basis for r in lost):.2f} USDC of cost basis gone)"
        ),
        code="unredeemed_lost",
        params={"count": len(lost),
                "amount": round(sum(r.cost_basis for r in lost), 2)},
        detail=(
            "They show as 'redeemable', but they resolved against you: redeeming a losing "
            "outcome token pays 0 USDC. It only clears them out of the position list — it is "
            "not recoverable money, and nothing in the portfolio view should be read as if it "
            "were."
        ),
        severity="info",
    )


def _insight_concentration(completed: list[_Result], settings: Settings | None) -> Insight | None:
    losers = sorted([r for r in completed if r.pnl < -_EPS], key=lambda r: r.pnl)
    total_loss = sum(r.pnl for r in losers)
    if not losers or total_loss >= -_EPS:
        return None
    top = losers[:2]
    top_loss = sum(r.pnl for r in top)
    share = top_loss / total_loss * 100
    if share < 40:
        return None
    biggest = max(completed, key=lambda r: r.cost_basis)
    detail = "; ".join(f"'{r.title}' {r.pnl:+.2f}" for r in top)
    extra = ""
    if settings is not None and biggest.cost_basis > settings.max_position_usdc:
        extra = (
            f" Largest single-market cost basis was {biggest.cost_basis:.2f} USDC "
            f"('{biggest.title}') — above the current max_position_usdc of "
            f"{settings.max_position_usdc:.2f}, so that trade could not be repeated "
            "under the configured limits."
        )
    return Insight(
        headline=f"Two markets produced {share:.0f}% of all losses ({top_loss:+.2f} USDC)",
        detail=f"{detail}.{extra} Losses are concentrated, not spread evenly across the book.",
        severity="warn",
        code="concentrated_losses",
        params={"share": round(share), "amount": round(top_loss, 2)},
    )


def _insight_execution(fills: list[_Fill]) -> Insight | None:
    if not fills:
        return None
    known = [f for f in fills if f.trader_side]
    takers = [f for f in known if str(f.trader_side).upper() == "TAKER"]
    if not known:
        return None
    pct = len(takers) / len(known) * 100
    if pct < 80:
        return None
    fee_fills = [f for f in fills if (f.fee_rate_bps or 0) > 0]
    fee_note = (
        f" {len(fee_fills)} of {len(fills)} fills also carried a non-zero fee rate "
        f"(up to {max(f.fee_rate_bps or 0 for f in fee_fills) / 100:.0f}%)."
        if fee_fills
        else ""
    )
    return Insight(
        headline=f"{len(takers)} of {len(known)} fills were taker fills ({pct:.0f}%)",
        code="taker_fills",
        params={"count": len(takers), "total": len(known), "pct": round(pct)},
        detail=(
            "Every taker fill pays the spread. On a 2c spread that is ~4% of a 50c contract "
            "per round trip, before any move in your favour. It also means none of these "
            "trades earned CLOB liquidity rewards, which only pay resting (maker) quotes near "
            "the midpoint." + fee_note
        ),
        severity="warn",
    )


def _insight_entry_prices(fills: list[_Fill]) -> Insight | None:
    buys = [f for f in fills if f.side.upper() == "BUY" and f.price > 0]
    if len(buys) < 5:
        return None
    high = [f for f in buys if f.price >= 0.90]
    low = [f for f in buys if f.price <= 0.10]
    if not high and not low:
        return None
    parts = []
    if high:
        # Best case on a high-priced buy is (1/price - 1); take the cheapest of
        # those entries so the number is the most generous one, not the worst.
        best_case = (1 / min(f.price for f in high) - 1) * 100
        parts.append(
            f"{len(high)} buys at 0.90 or above (best case +{best_case:.0f}%, full stake at risk)"
        )
    if low:
        parts.append(f"{len(low)} buys at 0.10 or below (longshots that need a rare event)")
    return Insight(
        headline=f"Entries cluster at the price extremes ({', '.join(parts)})",
        code="price_extremes",
        params={"detail": ", ".join(parts)},
        detail=(
            f"Average buy price across {len(buys)} fills was "
            f"{sum(f.notional for f in buys) / sum(f.size for f in buys):.3f}. "
            "Both extremes are legitimate trades, but they are opposite bets: near 1.00 you "
            "collect a few cents and risk the whole stake, near 0.00 you risk a small stake "
            "for a rare payout. Mixing them without sizing for it makes the P&L path lumpy "
            "and hard to read."
        ),
        severity="info",
    )


def _insight_best_result(completed: list[_Result]) -> Insight | None:
    wins = sorted([r for r in completed if r.pnl > _EPS], key=lambda r: r.pnl, reverse=True)
    if not wins:
        return None
    best = wins[0]
    runner = wins[1].pnl if len(wins) > 1 else 0.0
    others = sum(r.pnl for r in wins[1:])
    horizon = "short-horizon" if best.short_horizon else "multi-day news"
    comparison = (
        "more than every other winner combined"
        if best.pnl > others
        else "the largest single winner"
    )
    return Insight(
        headline=f"Best completed position: {best.pnl:+.2f} USDC on '{best.title}'",
        code="best_position",
        params={"amount": round(best.pnl, 2), "title": best.title},
        detail=(
            f"That single {horizon} market was {comparison} "
            f"(next best {runner:+.2f}, all other winners {others:+.2f} combined). "
            + (
                "The account's largest winner came from a market with days of runway rather "
                "than minutes — worth noting when deciding where to spend the next stake."
                if not best.short_horizon
                else "It is the same market type as the bulk of the losses, so treat it as "
                "one sample, not a pattern."
            )
        ),
        severity="good",
    )


def _insight_activity(fills: list[_Fill]) -> Insight | None:
    if not fills:
        return None
    stamped = [f for f in fills if f.timestamp]
    markets = {f.condition_id for f in fills}
    notional = sum(f.notional for f in fills)
    span = ""
    if stamped:
        first = min(f.timestamp for f in stamped)  # type: ignore[type-var]
        last = max(f.timestamp for f in stamped)  # type: ignore[type-var]
        span = f" between {first.date().isoformat()} and {last.date().isoformat()}"
    return Insight(
        headline=(
            f"{len(fills)} fills across {len(markets)} markets{span}, "
            f"{notional:.2f} USDC of total notional"
        ),
        code="activity",
        params={"fills": len(fills), "markets": len(markets),
                "notional": round(notional, 2)},
        detail=(
            f"Average {notional / len(fills):.2f} USDC per fill. Deduped by transaction hash "
            "across both the CLOB account-trade feed and the data-api trade feed, so a fill "
            "reported from both the maker and taker side is counted once."
        ),
        severity="info",
    )


def get_insights(client: SecureClient, *, settings: Settings | None = None) -> list[Insight]:
    """Plain-language read of the account's own trading record.

    Reports what the numbers say, including the losing patterns. It never
    predicts and never promises a fix — a pattern being structurally expensive
    is a fact about spreads and horizons, not a forecast.
    """
    results, fills = _collect_history(client)
    completed = [r for r in results if r.settled]

    if not completed and not fills:
        return [
            Insight(
                code="no_history",
                headline="No trading history yet",
                detail="No fills and no settled positions on this account, so there is "
                "nothing to analyse. Stats will appear after the first completed trade.",
                severity="info",
            )
        ]

    candidates: list[Insight | None] = []
    if completed:
        candidates.append(_insight_overall(_stats_from(completed)))
        candidates.append(_insight_short_horizon(completed))
        candidates.append(_insight_concentration(completed, settings))
    candidates.append(_insight_unredeemed(results))
    candidates.append(_insight_execution(fills))
    candidates.append(_insight_entry_prices(fills))
    if completed:
        candidates.append(_insight_best_result(completed))
    candidates.append(_insight_activity(fills))

    return [i for i in candidates if i is not None]


# --------------------------------------------------------------------------
# public API — opportunity screening (tradability only)
# --------------------------------------------------------------------------
_SCORE_NOTE = (
    "Score measures tradability only — spread, resting liquidity, traded volume, "
    "maker rewards and time left. It says nothing about which outcome will win."
)


def _ramp(value: float | None, low: float, high: float, weight: float, *, invert: bool = False) -> float:
    """Linear 0..weight ramp between low and high; invert when lower is better."""
    if value is None or high == low:
        return 0.0
    frac = min(1.0, max(0.0, (value - low) / (high - low)))
    return weight * (1.0 - frac if invert else frac)


def _log_ramp(value: float | None, low: float, high: float, weight: float) -> float:
    """Ramp on a log scale — money quantities span orders of magnitude."""
    if value is None or value <= 0:
        return 0.0
    return _ramp(math.log10(max(value, 1.0)), math.log10(low), math.log10(high), weight)


def _horizon_score(days: int | None, weight: float = 10.0) -> tuple[float, str]:
    if days is None:
        return 0.0, "No published end date — horizon unknown."
    if days < 0:
        return 0.0, "End date has passed; may be resolving."
    if days == 0:
        return weight * 0.1, "Resolves within a day — spread and fees dominate that horizon."
    if days <= 2:
        return weight * 0.5, f"~{days} day(s) left — very short horizon."
    if days <= 60:
        return weight, f"~{days} days left — enough runway to enter and exit deliberately."
    if days <= 180:
        return weight * 0.7, f"~{days} days left — capital tied up for months."
    return weight * 0.4, f"~{days} days left — long lockup for the capital."


def _pick_outcome(market: Market, min_price: float, max_price: float) -> tuple[Any, float] | None:
    """Pick the tradable side whose price sits inside the caller's band."""
    candidates: list[tuple[Any, float]] = []
    for outcome in (market.outcomes.yes, market.outcomes.no):
        if not outcome.token_id:
            continue
        price = _f(outcome.price)
        if price is None:
            continue
        candidates.append((outcome, price))
    if not candidates:
        return None
    in_band = [c for c in candidates if min_price <= c[1] <= max_price]
    if not in_band:
        return None
    # Prefer the side nearer the midpoint: less pinned, more two-way flow.
    return min(in_band, key=lambda c: abs(c[1] - 0.5))


def _matches_keyword(market: Market, keyword: str) -> bool:
    needle = keyword.lower()
    haystack = " ".join(
        part
        for part in (
            market.question,
            market.slug,
            market.group_item_title,
            *(t.label or "" for t in market.tags),
        )
        if part
    ).lower()
    return needle in haystack


def find_opportunities(
    client: SecureClient,
    *,
    limit: int = 25,
    max_price: float = 0.95,
    min_price: float = 0.02,
    keyword: str | None = None,
) -> list[Opportunity]:
    """Rank currently-tradable markets by how cheap they are to trade.

    Scoring is structural only, out of 100: absolute spread (20), spread as a
    share of the price (10), resting liquidity (20), 24h volume (20), maker
    reward rate (10), time to resolution (10), and how far the price sits from a
    pinned 0/1 (10). Nothing in the score is a forecast — a market can score 90
    and still be a bet you should not take.
    """
    sweep = 600 if keyword else min(600, max(200, limit * 20))
    markets = list_tradable_markets(client, limit=sweep)
    if keyword:
        markets = [m for m in markets if _matches_keyword(m, keyword)]

    picked: list[tuple[Market, Any, float]] = []
    for market in markets:
        if not market.condition_id:
            continue
        choice = _pick_outcome(market, min_price, max_price)
        if choice is None:
            continue
        liquidity = _f(market.metrics.liquidity_num)
        volume = _f(market.metrics.volume_24hr)
        if not liquidity and not volume:
            continue  # nothing resting and nothing traded: not really tradable
        picked.append((market, choice[0], choice[1]))

    # One batched spread lookup per chunk beats one call per market.
    live_spreads: dict[str, float] = {}
    token_ids = [str(outcome.token_id) for _m, outcome, _p in picked]
    for start in range(0, len(token_ids), _SPREAD_CHUNK):
        chunk = token_ids[start : start + _SPREAD_CHUNK]
        try:
            live_spreads.update(get_spreads(client, chunk))
        except Exception:
            continue  # fall back to the Gamma spread below

    opportunities: list[Opportunity] = []
    for market, outcome, price in picked:
        token_id = str(outcome.token_id)
        spread = live_spreads.get(token_id, _f(market.prices.spread))
        liquidity = _f(market.metrics.liquidity_num)
        volume = _f(market.metrics.volume_24hr)
        reward = daily_reward_rate(market)
        days, _note = time_to_resolution(market.state.end_date)

        # Absolute spread is what you pay; spread/price is what it costs you as a
        # fraction of the stake — a 1c spread is cheap at 0.50 and brutal at 0.03.
        rel_spread = spread / price if spread is not None and price > 0 else None
        spread_pts = _ramp(spread, 0.005, 0.06, 20.0, invert=True) if spread is not None else 0.0
        rel_pts = _ramp(rel_spread, 0.005, 0.10, 10.0, invert=True) if rel_spread is not None else 0.0
        liq_pts = _log_ramp(liquidity, 500, 50_000, 20.0)
        vol_pts = _log_ramp(volume, 100, 25_000, 20.0)
        reward_pts = _log_ramp(reward, 1, 50, 10.0)
        horizon_pts, horizon_note = _horizon_score(days)
        balance_pts = 10.0 * (1.0 - abs(price - 0.5) / 0.5)

        reasons: list[str] = []
        if spread is None:
            reasons.append("No spread published — one side of the book may be empty.")
        else:
            quality = "tight" if spread <= 0.01 else "workable" if spread <= 0.03 else "wide"
            reasons.append(
                f"Spread {spread * 100:.1f}c ({quality}) — that is what you give up crossing "
                f"it, {rel_spread * 100:.1f}% of the {price:.3f} price per crossing."
                if rel_spread is not None
                else f"Spread {spread * 100:.1f}c ({quality})."
            )
        if liquidity:
            reasons.append(f"~${liquidity:,.0f} resting liquidity in the book.")
        if volume:
            reasons.append(f"${volume:,.0f} traded in the last 24h — there is real flow here.")
        if reward >= 0.01:  # dust reward rates are noise, not a reason
            min_size = _f(market.rewards.rewards_min_size)
            reasons.append(
                f"Pays ~${reward:g}/day to resting quotes near the midpoint"
                + (f" (min size {min_size:g} shares)." if min_size else ".")
            )
        reasons.append(horizon_note)
        pinned = (
            " Pinned close to the edge of the price range: little room to move in your favour "
            "and a long way to fall."
            if price <= 0.10 or price >= 0.90
            else ""
        )
        reasons.append(
            f"{outcome.label} priced at {price:.3f} (~{price * 100:.0f}% implied by the market, "
            f"which is a crowd estimate, not this tool's view).{pinned}"
        )
        min_order = _f(market.trading.minimum_order_size)
        if min_order and min_order * price > 1.0:
            reasons.append(
                f"Minimum order {min_order:g} shares — at least ${min_order * price:.2f} to enter."
            )
        reasons.append(_SCORE_NOTE)

        opportunities.append(
            Opportunity(
                condition_id=str(market.condition_id),
                token_id=token_id,
                market_title=market.question or market.slug or str(market.condition_id),
                slug=market.slug,
                outcome=outcome.label,
                price=round(price, 4),
                spread=round(spread, 4) if spread is not None else None,
                liquidity=round(liquidity, 2) if liquidity is not None else None,
                volume_24h=round(volume, 2) if volume is not None else None,
                daily_reward=round(reward, 2),
                days_left=days,
                score=round(
                    spread_pts
                    + rel_pts
                    + liq_pts
                    + vol_pts
                    + reward_pts
                    + horizon_pts
                    + balance_pts,
                    1,
                ),
                reasons=reasons,
            )
        )

    opportunities.sort(key=lambda o: o.score, reverse=True)
    return opportunities[:limit]
