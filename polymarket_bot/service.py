"""The facade a CLI - or a Telegram bot - calls. One function per user intent.

Everything above this line is presentation; everything below it is logic. That
split is the whole reason the bot can be driven from a chat window: a Telegram
handler only has to turn a message into one call here and print the answer.

The contract every function in this module keeps:

  * **It returns a JSON-safe dict and never raises.** Expected failures (bad
    slug, no such position, over a risk limit) come back as
    ``{"ok": False, "error": "..."}``; unexpected ones are caught by `_safe` and
    reported the same way. A bad user command must never kill the handler.
  * **`text` is always present and always renderable.** A handler can reply with
    `response["text"]` and be done - no formatting logic on the transport side.
    It is deliberately plain ASCII: the same string goes to a Windows console
    and to a chat, and neither should mangle it.
  * **The client is opened and closed per call** (`finally`, always), unless the
    caller passes one in with `client=` - then it belongs to the caller and is
    left open. That keeps a one-shot Telegram command from leaking sockets while
    still letting a script reuse one connection for a whole session.
  * **Anything a call emits is captured**, not printed. Calls that produce events
    (buy, sell, redeem, monitor) run a `CollectingNotifier` and return the
    messages in `events`, so nothing is lost to a stdout nobody is watching.

`buy` and `sell` refuse without `confirm=True` and hand back the full preview
plus `needs_confirmation`, so a chat UI can show the exact plan, then confirm it.
The confirm leg must send the size back, not the original request: the
unconfirmed response carries `confirm_args` (`shares=` + `expected_shares=`, or
`usd=` + `expected_usdc=`) and passing those binds the confirmation to the plan
that was actually shown. Every plan is rebuilt from live state immediately
before it is sent - correct, but it means a resting buy filling in the seconds
between the preview and the button can grow the order. `expected_*` refuses
anything larger than what was approved.

Nothing here predicts anything. Previews state mechanics (cost, size, what the
exchange or our own limits object to); scans and opportunity ranking state
tradability. Neither is a claim about which outcome wins.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from typing import Any, ParamSpec

from polymarket import Market, SecureClient

from polymarket_bot import advisor, portfolio, trading
from polymarket_bot.analytics import find_opportunities, get_insights, get_trade_stats
from polymarket_bot.client import get_client
from polymarket_bot.config import Settings, load_settings
from polymarket_bot.markets import (
    BookSnapshot,
    daily_reward_rate,
    get_book_snapshot,
    get_market_by_condition_id,
    get_market_by_slug,
    get_spreads,
    is_tradable,
    list_tradable_markets,
    market_url,
)
from polymarket_bot.notify import CollectingNotifier
from polymarket_bot.rules import VALID_KINDS, ExitRule, RuleStore, evaluate_rule, make_rule
from polymarket_bot.trading import OrderPlan

_P = ParamSpec("_P")

# get_spreads takes a batch; the data-api rejects very large ones.
_SPREAD_CHUNK = 50

# Keyword scans have to sweep a lot of markets to find a few matches, but the
# sweep is paginated network I/O - keep it bounded so a chat command stays snappy.
# How many live markets a keyword search walks before giving up. This was 400,
# which put "Will the U.S. invade Iran before 2027?" (index ~500, $161k 24h
# volume) outside the window: `--search iran` reported "no markets matched"
# while `analyze --keyword iran` (which swept 600) listed four. A search that
# reports absence must actually have looked, and when it truncates it has to
# say so - see the "scanned the first N" message below.
_KEYWORD_SWEEP = 1500

_OPPORTUNITY_BASIS = (
    "Ranked on tradability only - spread, resting liquidity, 24h volume, maker "
    "reward rate and time to resolution. It says nothing about which outcome wins."
)

# --- confirmation binding --------------------------------------------------
# A confirmation approves ONE SPECIFIC SIZE. Every plan is (correctly) rebuilt
# from live state before it is sent, so between "Sell: 10.00 shares" and the
# user pressing Confirm a resting buy can fill and the rebuilt plan can ask to
# sell 15.00 - shares nobody approved. `expected_shares` / `expected_usdc` carry
# the confirmed number back in and bound how far the rebuild may drift UPWARDS.
# Drifting down is allowed: it can only ever sell or spend less than approved.
_SHARES_EPSILON = 0.005  # half the exchange's 0.01-share size grid
_USDC_EPSILON = 0.01  # one cent


# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------
#: The four keys the envelope owns. `ok` in particular is what every caller -
#: a script's exit code, a Telegram handler's success/failure branch - reads,
#: so it must mean what this module decided and nothing else.
_RESERVED_KEYS = ("ok", "action", "error", "text")


def _payload_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Extra response fields, with the envelope's own keys quarantined.

    Splatting a foreign dict into `_ok`/`_fail` was a live bug: several of the
    dicts this module forwards (`trading.cancel_all_orders`, anything modelled
    on it) carry their own `ok`/`error`, so `**outcome` either raised
    `TypeError: _fail() got multiple values for argument 'error'` - destroying
    the real exchange error on the way - or quietly overwrote `ok=True` with
    the inner `ok=False`, producing a response that read "Cancelled 1 resting
    order(s)." while `ok=False, error=None`. A handler branching on `ok`
    reported a failure with no reason.

    Two things stop that recurring: `action` and `error` are positional-only
    below, so a splat can no longer bind to them or collide with them, and
    anything that reaches `**fields` under a reserved name is preserved as
    `<key>_detail` rather than overwriting the envelope or being dropped - the
    inner value is usually the interesting one. Call sites still pass fields
    explicitly; this is the backstop, not the policy.
    """
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        clean[f"{key}_detail" if key in _RESERVED_KEYS else key] = value
    return clean


def _ok(action: str, text: str, /, **fields: Any) -> dict:
    payload: dict[str, Any] = {"ok": True, "action": action, "error": None, "text": text}
    payload.update(_payload_fields(fields))
    return payload


def _fail(action: str, error: str, /, *, text: str | None = None, **fields: Any) -> dict:
    payload: dict[str, Any] = {
        "ok": False,
        "action": action,
        "error": error,
        "text": text if text is not None else f"{action} failed: {error}",
    }
    payload.update(_payload_fields(fields))
    return payload


def _safe(fn: Callable[_P, dict]) -> Callable[_P, dict]:
    """Guarantee that a service call returns a dict instead of raising.

    Expected failures are already returned as `ok=False` inside each function;
    this is the backstop for the unexpected ones (a dropped connection, an SDK
    model change). A Telegram handler has nowhere to put a traceback.
    """

    @wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            # ValueError is how this package reports bad user input (unknown
            # outcome, unresolvable market). Those messages are already written
            # for a human - don't bury them behind a type name.
            return _fail(fn.__name__, str(exc))
        except Exception as exc:
            return _fail(fn.__name__, f"{type(exc).__name__}: {exc}")

    return wrapper


@contextmanager
def _session(client: SecureClient | None) -> Iterator[tuple[SecureClient, Settings]]:
    """Yield (client, settings), closing the client only if we opened it."""
    settings = load_settings()
    if client is not None:
        # Borrowed connection: the caller owns its lifetime, so don't close it.
        yield client, settings
        return
    own = get_client()
    try:
        yield own, settings
    finally:
        try:
            own.close()
        except Exception:
            pass  # a failed close must not turn a completed call into an error


def _events(collector: CollectingNotifier) -> list[dict]:
    return [{"level": level, "message": message} for level, message in collector.messages]


def _events_text(events: list[dict]) -> str:
    # ASCII only; notify.py's own text() uses emoji prefixes that a non-UTF-8
    # Windows console will not render.
    return "\n".join(f"  [{e['level']}] {e['message']}" for e in events)


# --------------------------------------------------------------------------
# resolution helpers
# --------------------------------------------------------------------------
def _num(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


def _resolve_market(client: SecureClient, market_ref: str) -> Market:
    """Condition id (0x...), slug, or a pasted polymarket.com URL -> Market.

    Resolved once per call and reused, because each of these is a network round
    trip and a chat user will happily paste any of the three.
    """
    ref = (market_ref or "").strip()
    if not ref:
        raise ValueError("No market given - pass a condition id (0x...), a slug, or a market URL.")

    attempts: list[Callable[[], Market]]
    if ref.startswith(("http://", "https://")):
        attempts = [lambda: client.get_market(url=ref)]
    elif ref.startswith("0x"):
        attempts = [lambda: get_market_by_condition_id(client, ref)]
    else:
        # Slug first, then condition id: a bare hex-less string is almost always
        # a slug, but falling back costs one request and saves a confused user.
        attempts = [
            lambda: get_market_by_slug(client, ref),
            lambda: get_market_by_condition_id(client, ref),
        ]

    last: str | None = None
    for attempt in attempts:
        try:
            market = attempt()
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if market is not None:
            return market
    # The failed attempt is reported as context, not as the reason: after a
    # fallback it names the *last* lookup tried, which is rarely what was meant.
    raise ValueError(
        f"No market matches {ref!r}. Pass a condition id (0x...), a market slug, or a "
        "polymarket.com URL." + (f" Last lookup error: {last}" if last else "")
    )


def _outcome_of(market: Market, outcome: str):
    """Map "yes"/"no" - or the market's own label - onto a MarketOutcome.

    trading.py resolves outcomes itself when building a plan; this exists for the
    paths that need a token id without building one (rules). Real markets rarely
    say Yes/No - "Bitcoin Up or Down" labels them Up/Down - so both work.
    """
    wanted = (outcome or "").strip().lower()
    yes, no = market.outcomes.yes, market.outcomes.no
    if wanted in ("yes", "y", "1"):
        return yes
    if wanted in ("no", "n", "0"):
        return no
    for side in (yes, no):
        if side.label and side.label.strip().lower() == wanted:
            return side
    raise ValueError(
        f"Unknown outcome {outcome!r}; this market offers '{yes.label}' (yes) or '{no.label}' (no)."
    )


def _title(market: Market) -> str:
    return market.question or market.slug or str(market.condition_id or "")


def _snapshot_dict(snapshot: BookSnapshot | None) -> dict | None:
    # BookSnapshot.to_dict() is the module's own JSON-safe view and already
    # covers reachable vs total depth; re-listing the fields here only creates
    # somewhere for them to go stale.
    return None if snapshot is None else snapshot.to_dict()


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _signed(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


# --------------------------------------------------------------------------
# account
# --------------------------------------------------------------------------
@_safe
def status(*, client: SecureClient | None = None) -> dict:
    """Cash, portfolio totals, resting orders, active rules, monitor mode."""
    with _session(client) as (api, settings):
        summary = portfolio.get_portfolio_summary(api)
        try:
            rules = RuleStore(settings).list(active_only=True)
            rules_error: str | None = None
        except Exception as exc:
            # A local JSON file momentarily held by a backup/antivirus agent
            # must not take down the account view. The balance and positions
            # are the point of this call; the rule count is context.
            rules, rules_error = [], f"{type(exc).__name__}: {exc}"
        try:
            open_orders = trading.list_open_orders(api)
            orders_error: str | None = None
        except Exception as exc:
            # Resting orders are a nice-to-have here; the balance is the point.
            open_orders, orders_error = [], f"{type(exc).__name__}: {exc}"

    orders_line = (
        f"could not be read ({orders_error})" if orders_error else f"{len(open_orders)} resting"
    )
    rules_line = (
        f"could not be read ({rules_error}) - exits may be unmonitored"
        if rules_error
        else f"{len(rules)} active"
    )
    redeem_note = ""
    if summary.redeemable_count:
        redeem_note = (
            f" worth {_usd(summary.redeemable_value)}"
            + (" - a resolved loser redeems for $0" if summary.redeemable_value < 0.01 else "")
        )

    # `unrealized_pnl` is live positions only since portfolio.py split the book;
    # settled P&L is decided and cannot move, so it is reported on its own line
    # rather than folded in. Printing only the live number is what produced
    # "open=0, unrealized=-13.93", which reads as a contradiction.
    settled_line = (
        f"  Settled     : {summary.settled_positions} position(s) worth "
        f"{_usd(summary.settled_value)}, P&L {_signed(summary.settled_pnl)} (decided - redeem, do not sell)"
    )

    text = "\n".join(
        [
            "ACCOUNT",
            f"  Cash        : {_usd(summary.cash_usdc)} USDC",
            f"  Positions   : {_usd(summary.open_positions_value)} across {summary.open_positions} open",
            f"  Total value : {_usd(summary.total_value)} (all holdings {_usd(summary.positions_value)} + cash)",
            # Written as a sum you can verify by eye. The old line put three
            # numbers with three different denominators side by side, so
            # "unrealized +$0.00 | realized +$2.18 | total -$13.93" looked like
            # broken arithmetic. Only open + settled feeds total; realized is a
            # separate figure and now says so on its own line.
            f"  P&L         : open {_signed(summary.unrealized_pnl)} + settled "
            f"{_signed(summary.settled_pnl)} = {_signed(summary.total_pnl)}",
            *([settled_line] if summary.settled_positions else []),
            *(
                [
                    f"  Realized    : {_signed(summary.realized_pnl)} already banked on exits "
                    f"taken before settlement (separate from the line above)"
                ]
                if abs(summary.realized_pnl) >= 0.005
                else []
            ),
            f"  Redeemable  : {summary.redeemable_count} position(s){redeem_note}",
            f"  Open orders : {orders_line}",
            f"  Exit rules  : {rules_line}",
            f"  Monitor     : {'DRY RUN - evaluates only, sends nothing' if settings.monitor_dry_run else 'LIVE - can send sell orders'}",
            f"  Limits      : max order {_usd(settings.max_order_usdc)} (BUYs only - a sell reduces risk) | "
            f"max per market {_usd(settings.max_position_usdc)} | "
            f"cash reserve {_usd(settings.min_cash_reserve_usdc)}",
        ]
    )

    return _ok(
        "status",
        text,
        portfolio=summary.to_dict(),
        open_orders=open_orders,
        open_orders_error=orders_error,
        active_rules=len(rules),
        rules_error=rules_error,
        monitor_dry_run=settings.monitor_dry_run,
        limits={
            "max_order_usdc": settings.max_order_usdc,
            "max_position_usdc": settings.max_position_usdc,
            "daily_loss_limit_usdc": settings.daily_loss_limit_usdc,
            "min_cash_reserve_usdc": settings.min_cash_reserve_usdc,
        },
    )


@_safe
def positions(*, include_resolved: bool = False, client: SecureClient | None = None) -> dict:
    """Current holdings. Settled markets are hidden unless `include_resolved`."""
    with _session(client) as (api, _settings):
        views = portfolio.get_positions(api, include_resolved=include_resolved)

    if not views:
        text = (
            "No open positions."
            if not include_resolved
            else "No positions at all on this account."
        )
        if not include_resolved:
            text += " (Settled positions are hidden; pass include_resolved=True to see them.)"
        # Same keys as the populated branch: a caller should never have to
        # branch on whether the list happened to be empty.
        return _ok(
            "positions",
            text,
            positions=[],
            count=0,
            include_resolved=include_resolved,
            total_value=0.0,
            total_unrealized_pnl=0.0,
        )

    lines = ["POSITIONS" + (" (including settled)" if include_resolved else "")]
    for index, view in enumerate(views, start=1):
        flag = " [SETTLED - redeem, do not sell]" if view.is_resolved else ""
        lines.append(f"{index}. {_clip(view.market_title, 60)}{flag}")
        lines.append(
            f"     {view.outcome}: {view.shares:,.2f} sh @ {view.avg_price:.4f} -> {view.cur_price:.4f}"
            f" | value {_usd(view.current_value)}"
            f" | P&L {_signed(view.unrealized_pnl)} ({view.unrealized_pnl_pct:+.1f}%)"
        )
    total_value = sum(v.current_value for v in views)
    total_pnl = sum(v.unrealized_pnl for v in views)
    lines.append(f"  Total: {_usd(total_value)} | unrealized {_signed(total_pnl)}")

    return _ok(
        "positions",
        "\n".join(lines),
        positions=[v.to_dict() for v in views],
        count=len(views),
        include_resolved=include_resolved,
        total_value=round(total_value, 6),
        total_unrealized_pnl=round(total_pnl, 6),
    )


# --------------------------------------------------------------------------
# market data
# --------------------------------------------------------------------------
@_safe
def scan(
    *,
    limit: int = 20,
    keyword: str | None = None,
    sort: str = "spread",
    client: SecureClient | None = None,
) -> dict:
    """Tradable markets. Ranks tradability or activity - never odds.

    `sort="spread"` (the default, and what every existing caller gets) puts the
    tightest spread first: cheapest to get in and out of.

    `sort="hot"` orders by 24h volume instead - how much money actually moved.
    That is popularity, not edge: a market can be the most traded on the venue
    and still be a bad bet. It also makes keyword search materially better,
    because the bounded sweep then walks markets people are trading rather than
    an arbitrary slice of the venue.
    """
    count = max(1, int(limit))
    needle = (keyword or "").strip().lower() or None
    hot = str(sort).lower() == "hot"

    with _session(client) as (api, _settings):
        sweep = _KEYWORD_SWEEP if needle else count
        markets = list_tradable_markets(
            api,
            limit=sweep,
            # Only "hot" asks the API to order; "spread" is sorted below from
            # live book data, which the API cannot sort by.
            order="volume24hr" if hot else None,
            ascending=False if hot else None,
        )
        scanned = len(markets)
        truncated = needle is not None and scanned >= sweep
        if needle:
            markets = [
                m
                for m in markets
                if needle in " ".join(p for p in (m.question, m.slug, m.group_item_title) if p).lower()
            ][:count]

        tokens = [str(m.outcomes.yes.token_id) for m in markets if m.outcomes.yes.token_id]
        live_spreads: dict[str, float] = {}
        for start in range(0, len(tokens), _SPREAD_CHUNK):
            try:
                live_spreads.update(get_spreads(api, tokens[start : start + _SPREAD_CHUNK]))
            except Exception:
                continue  # fall back to the gamma spread below

    rows: list[dict] = []
    for market in markets:
        yes, no = market.outcomes.yes, market.outcomes.no
        token_id = str(yes.token_id) if yes.token_id else None
        spread = live_spreads.get(token_id or "", _num(market.prices.spread))
        days, _note = advisor.time_to_resolution(market.state.end_date)
        rows.append(
            {
                "condition_id": str(market.condition_id or ""),
                "slug": market.slug,
                "question": _title(market),
                "yes": {"label": yes.label, "token_id": token_id, "price": _num(yes.price)},
                "no": {
                    "label": no.label,
                    "token_id": str(no.token_id) if no.token_id else None,
                    "price": _num(no.price),
                },
                "spread": round(spread, 4) if spread is not None else None,
                "volume_24h": _num(market.metrics.volume_24hr),
                "liquidity": _num(market.metrics.liquidity_num),
                "daily_reward": daily_reward_rate(market),
                "days_left": days,
                "end_date": market.state.end_date.isoformat() if market.state.end_date else None,
                "accepting_orders": is_tradable(market),
                "url": market_url(market),
            }
        )

    if hot:
        # The API already returned these volume-ordered; re-sorting here keeps
        # the contract explicit and survives the keyword filter above, which
        # preserves order but not the guarantee. None last, as below.
        rows.sort(key=lambda r: (r["volume_24h"] is None, -(r["volume_24h"] or 0.0)))
    else:
        # None sorts last: an unknown spread is not evidence of a good one.
        rows.sort(
            key=lambda r: (r["spread"] is None, r["spread"] if r["spread"] is not None else 9.9)
        )
    rows = rows[:count]

    if not rows:
        if needle:
            # Never a bare "no markets matched": this search walks a bounded
            # window, so absence from it is not absence from Polymarket.
            empty = f"No match for {keyword!r} in the {scanned:,} live markets scanned"
            empty += (
                " - that is the scan limit, so there may be matches beyond it. "
                "Try a more specific keyword, or `analyze --keyword` which ranks by tradability."
                if truncated
                else " (the full live set)."
            )
        else:
            empty = "No tradable markets returned."
        return _ok(
            "scan",
            empty,
            markets=[],
            count=0,
            keyword=keyword,
            scanned=scanned,
            truncated=truncated,
            disclaimer=advisor.DISCLAIMER,
        )

    lines = [f"{'YES':>5}  {'SPREAD':>7}  {'24H VOL':>10}  QUESTION"]
    lines.append("-" * 78)
    for row in rows:
        price = row["yes"]["price"]
        price_str = f"{round(price * 100):>4}%" if price is not None else "   ?"
        spread_str = f"{row['spread'] * 100:.1f}c" if row["spread"] is not None else "?"
        volume = row["volume_24h"]
        volume_str = f"${volume:,.0f}" if volume else "-"
        lines.append(f"{price_str:>5}  {spread_str:>7}  {volume_str:>10}  {_clip(row['question'], 44)}")
    lines.append("")
    if hot:
        lines.append("Sorted by 24h volume: how much money moved. That is popularity,")
        lines.append("not edge - the most traded market can still be a bad bet.")
    else:
        lines.append("Sorted by spread: tightest = cheapest to get in and out of. That is")
        lines.append("tradability, not a view on which side wins.")
    lines.append(f"! {advisor.DISCLAIMER}")

    return _ok(
        "scan",
        "\n".join(lines),
        markets=rows,
        count=len(rows),
        keyword=keyword,
        sort="hot" if hot else "spread",
        disclaimer=advisor.DISCLAIMER,
    )


@_safe
def briefing(market_ref: str, *, client: SecureClient | None = None) -> dict:
    """Educational read of one market: price, horizon, liquidity, rewards."""
    with _session(client) as (api, _settings):
        market = _resolve_market(api, market_ref)
        token_id = market.outcomes.yes.token_id
        snapshot: BookSnapshot | None = None
        book_error: str | None = None
        if token_id:
            try:
                snapshot = get_book_snapshot(api, str(token_id))
            except Exception as exc:
                book_error = f"{type(exc).__name__}: {exc}"
        note = advisor.briefing(market, snapshot)

    lines = list(note.lines)
    if book_error:
        lines.append(f"Book unavailable ({book_error}) - liquidity could not be assessed.")

    text = "\n".join(
        [f"* {note.question}", *(f"  {line}" for line in lines), f"  ! {advisor.DISCLAIMER}"]
    )

    return _ok(
        "briefing",
        text,
        question=note.question,
        slug=note.slug,
        condition_id=str(market.condition_id or ""),
        lines=lines,
        book=_snapshot_dict(snapshot),
        accepting_orders=is_tradable(market),
        url=market_url(market),
        disclaimer=advisor.DISCLAIMER,
    )


@_safe
def opportunities(
    *, limit: int = 15, keyword: str | None = None, client: SecureClient | None = None
) -> dict:
    """Markets ranked by how cheap they are to trade. Never a prediction."""
    with _session(client) as (api, _settings):
        found = find_opportunities(api, limit=max(1, int(limit)), keyword=keyword)

    if not found:
        return _ok(
            "opportunities",
            f"No tradable markets matched {keyword!r}." if keyword else "No markets scored.",
            opportunities=[],
            count=0,
            keyword=keyword,
            basis=_OPPORTUNITY_BASIS,
            disclaimer=advisor.DISCLAIMER,
        )

    lines = ["MOST TRADABLE MARKETS (structure only)"]
    for index, item in enumerate(found, start=1):
        spread_str = f"{item.spread * 100:.1f}c" if item.spread is not None else "?"
        volume_str = f"${item.volume_24h:,.0f}" if item.volume_24h else "-"
        horizon = f"{item.days_left}d left" if item.days_left is not None else "no end date"
        lines.append(f"{index}. [{item.score:>5.1f}] {_clip(item.market_title, 58)}")
        lines.append(
            f"     {item.outcome} @ {item.price:.3f} | spread {spread_str}"
            f" | 24h vol {volume_str} | {horizon}"
            + (f" | rewards ${item.daily_reward:g}/day" if item.daily_reward else "")
        )
    lines.append("")
    lines.append(_OPPORTUNITY_BASIS)
    lines.append(f"! {advisor.DISCLAIMER}")

    return _ok(
        "opportunities",
        "\n".join(lines),
        opportunities=[o.to_dict() for o in found],
        count=len(found),
        keyword=keyword,
        basis=_OPPORTUNITY_BASIS,
        disclaimer=advisor.DISCLAIMER,
    )


@_safe
def analytics(*, client: SecureClient | None = None) -> dict:
    """The account's own record - wins, losses, and the patterns behind them."""
    with _session(client) as (api, settings):
        stats = get_trade_stats(api)
        insights = get_insights(api, settings=settings)

    lines = [
        "TRADING RECORD (completed positions, realized P&L only)",
        # Show the break-even bucket when it exists, otherwise wins + losses
        # silently fail to add up to the total and the header looks wrong.
        f"  Completed : {stats.total_trades}  ({stats.wins} win / {stats.losses} loss"
        + (
            f" / {stats.total_trades - stats.wins - stats.losses} break-even)"
            if stats.total_trades - stats.wins - stats.losses
            else ")"
        ),
        f"  Hit rate  : {stats.win_rate * 100:.0f}%",
        f"  Net P&L   : {_signed(stats.total_pnl)} USDC",
        f"  Averages  : winner {_signed(stats.avg_win)} | loser {_signed(stats.avg_loss)}",
    ]
    if stats.best:
        lines.append(f"  Best      : {stats.best.get('pnl', 0):+.2f} on {_clip(str(stats.best.get('market', '')), 48)}")
    if stats.worst:
        lines.append(f"  Worst     : {stats.worst.get('pnl', 0):+.2f} on {_clip(str(stats.worst.get('market', '')), 48)}")
    if insights:
        lines.append("")
        lines.append("WHAT THE HISTORY SHOWS")
        for insight in insights:
            lines.append(f"  [{insight.severity}] {insight.headline}")
            lines.append(f"     {insight.detail}")

    return _ok(
        "analytics",
        "\n".join(lines),
        stats=stats.to_dict(),
        insights=[i.to_dict() for i in insights],
        disclaimer=advisor.DISCLAIMER,
    )


# --------------------------------------------------------------------------
# trading
# --------------------------------------------------------------------------
def _preview_payload(action: str, plan: OrderPlan, *, suffix: str) -> dict:
    return _ok(
        action,
        f"{plan.to_text()}\n  {suffix}",
        plan=plan.to_dict(),
        executable=plan.is_executable(),
        warnings=list(plan.warnings),
        blockers=list(plan.blockers),
    )


def _refused(action: str, plan: OrderPlan) -> dict:
    reason = "; ".join(plan.blockers)
    return _fail(
        action,
        f"Refused: {reason}",
        text=f"{plan.to_text()}\n  NOTHING SENT.",
        plan=plan.to_dict(),
        executable=False,
        warnings=list(plan.warnings),
        blockers=list(plan.blockers),
        needs_confirmation=False,
    )


def _confirm_args(plan: OrderPlan) -> dict:
    """Exactly what the confirm leg must pass back to execute THIS plan.

    A caller (a Telegram Confirm button, a CLI prompt) should splat this into
    `buy`/`sell` rather than rebuilding the arguments from whatever the user
    originally typed. `fraction` is deliberately absent: "sell all" re-resolves
    against the live position, which is how a confirmation for 10 shares turns
    into a sale of 15. `limit_price` is included even when it is None - dropping
    it would silently turn a confirmed resting limit order into a market order.
    """
    if plan.side == "BUY":
        return {
            "usd": plan.usdc_amount,
            "expected_usdc": plan.usdc_amount,
            "limit_price": plan.limit_price,
            "confirm": True,
        }
    return {
        "shares": plan.shares,
        "expected_shares": plan.shares,
        "limit_price": plan.limit_price,
        "confirm": True,
    }


def _unconfirmed(action: str, plan: OrderPlan) -> dict:
    return _fail(
        action,
        "Confirmation required - nothing was sent.",
        text=f"{plan.to_text()}\n  CONFIRM  : nothing sent yet. Re-run with confirm=True to execute.",
        plan=plan.to_dict(),
        executable=plan.is_executable(),
        warnings=list(plan.warnings),
        blockers=list(plan.blockers),
        needs_confirmation=True,
        confirm_args=_confirm_args(plan),
    )


def _guard_confirmed(action: str, plan: OrderPlan, confirmed: float | None) -> dict | None:
    """Refuse a plan that grew past the size that was actually confirmed.

    Returns a failure response to hand straight back, or None to proceed. Only
    growth is refused - a plan that shrank (the owner sold some elsewhere) sells
    less than was approved, which needs no new approval, so it is annotated and
    allowed through.
    """
    if confirmed is None:
        return None

    is_buy = plan.side == "BUY"
    label = "expected_usdc" if is_buy else "expected_shares"
    epsilon = _USDC_EPSILON if is_buy else _SHARES_EPSILON
    actual = plan.usdc_amount if is_buy else plan.shares

    def show(value: float) -> str:
        return f"${value:,.2f} USDC" if is_buy else f"{value:,.2f} shares"

    if not isinstance(confirmed, (int, float)) or not math.isfinite(confirmed) or confirmed <= 0:
        return _fail(
            action,
            f"{label} must be a positive, finite number; got {confirmed!r}. Nothing sent.",
            plan=plan.to_dict(),
            executed=False,
            needs_confirmation=True,
        )
    if actual is None:
        return _fail(
            action,
            f"This plan has no size to check against {label}={show(float(confirmed))}. Nothing sent.",
            plan=plan.to_dict(),
            executed=False,
            needs_confirmation=True,
        )

    if actual > float(confirmed) + epsilon:
        text = "\n".join(
            [
                f"{plan.side} REFUSED - the order grew after it was confirmed.",
                f"  Market    : {plan.market_title}",
                f"  Confirmed : {show(float(confirmed))}",
                f"  Now asks  : {show(actual)}",
                "  Live state changed between the preview and the confirmation, so this",
                "  would trade more than was approved. NOTHING SENT - preview again and",
                "  confirm the new size if you still want it.",
            ]
        )
        return _fail(
            action,
            f"Refused: plan grew to {show(actual)} after {show(float(confirmed))} was confirmed - "
            "live state changed, nothing sent.",
            text=text,
            plan=plan.to_dict(),
            executed=False,
            needs_confirmation=True,
            confirmed_amount=round(float(confirmed), 6),
            plan_amount=round(float(actual), 6),
            confirm_args=_confirm_args(plan),
        )

    if actual < float(confirmed) - epsilon:
        # Smaller than approved: allowed, but never silently. It usually means
        # the position shrank between preview and confirmation.
        plan.warnings.append(
            f"Sending {show(actual)} - less than the {show(float(confirmed))} confirmed, "
            "because live state changed since the preview."
        )
    return None


def _executed(action: str, result: trading.TradeResult, events: list[dict]) -> dict:
    plan = result.plan
    if result.ok:
        if result.filled_shares > 0:
            headline = (
                f"{plan.side} filled {result.filled_shares:,.2f} '{plan.outcome_label}' shares "
                f"for {_usd(result.filled_usdc)}"
                + (f" @ ~{result.avg_price:.4f}" if result.avg_price is not None else "")
            )
        elif plan.kind == "limit":
            headline = f"Limit order is resting on the book, unfilled (order {result.order_id})."
        else:
            headline = "Order accepted but nothing matched - no fill."
        text = "\n".join(
            [f"{headline}", f"  Market   : {plan.market_title}", f"  Status   : {result.status or 'accepted'}"]
            + ([_events_text(events)] if events else [])
        )
        return _ok(
            action,
            text,
            result=result.to_dict(),
            plan=plan.to_dict(),
            executed=True,
            events=events,
        )
    return _fail(
        action,
        result.error or "Order failed.",
        text="\n".join(
            [f"{plan.side} FAILED - {plan.market_title}", f"  {result.error or 'unknown error'}"]
            + ([_events_text(events)] if events else [])
        ),
        result=result.to_dict(),
        plan=plan.to_dict(),
        executed=False,
        events=events,
        needs_confirmation=False,
    )


@_safe
def preview_buy(
    market_ref: str,
    outcome: str,
    usd: float,
    *,
    limit_price: float | None = None,
    client: SecureClient | None = None,
) -> dict:
    """Price and vet a BUY without sending it. `ok` means the preview was built;
    check `executable` for whether it would be allowed through."""
    with _session(client) as (api, settings):
        market = _resolve_market(api, market_ref)
        plan = trading.build_buy_plan(
            api,
            settings,
            market=market,
            outcome=outcome,
            usdc_amount=float(usd),
            limit_price=limit_price,
        )
    return _preview_payload("preview_buy", plan, suffix="PREVIEW  : nothing has been sent.")


@_safe
def buy(
    market_ref: str,
    outcome: str,
    usd: float,
    *,
    limit_price: float | None = None,
    confirm: bool = False,
    expected_usdc: float | None = None,
    client: SecureClient | None = None,
) -> dict:
    """Buy an outcome. Refuses unless `confirm=True`, returning the preview.

    `expected_usdc` binds the confirmation to a specific spend: pass the
    `usdc_amount` from the plan that was shown, and this refuses to send if the
    rebuilt plan asks for more than that (plus one cent of tolerance).
    `_unconfirmed` hands back exactly the right arguments in `confirm_args`.
    """
    with _session(client) as (api, settings):
        market = _resolve_market(api, market_ref)
        plan = trading.build_buy_plan(
            api,
            settings,
            market=market,
            outcome=outcome,
            usdc_amount=float(usd),
            limit_price=limit_price,
        )
        # Blockers first: a plan that can never execute is not worth confirming,
        # so report the real reason instead of prompting for a pointless yes.
        if plan.blockers:
            return _refused("buy", plan)
        if not confirm:
            return _unconfirmed("buy", plan)
        drifted = _guard_confirmed("buy", plan, expected_usdc)
        if drifted is not None:
            return drifted

        collector = CollectingNotifier()
        result = trading.execute_plan(api, settings, plan, notifier=collector)
    return _executed("buy", result, _events(collector))


@_safe
def preview_sell(
    market_ref: str,
    outcome: str,
    *,
    shares: float | None = None,
    fraction: float | None = None,
    limit_price: float | None = None,
    client: SecureClient | None = None,
) -> dict:
    """Price and vet a SELL without sending it. Exactly one of shares/fraction."""
    with _session(client) as (api, settings):
        market = _resolve_market(api, market_ref)
        plan = trading.build_sell_plan(
            api,
            settings,
            market=market,
            outcome=outcome,
            shares=shares,
            fraction=fraction,
            limit_price=limit_price,
        )
    return _preview_payload("preview_sell", plan, suffix="PREVIEW  : nothing has been sent.")


@_safe
def sell(
    market_ref: str,
    outcome: str,
    *,
    shares: float | None = None,
    fraction: float | None = None,
    limit_price: float | None = None,
    confirm: bool = False,
    expected_shares: float | None = None,
    client: SecureClient | None = None,
) -> dict:
    """Sell an outcome you hold. Refuses unless `confirm=True`.

    A confirm leg must pass `shares=` (the exact number from the plan that was
    shown) and `expected_shares=` the same value - never `fraction`. `fraction`
    re-resolves against the live position at execution time, so a resting buy
    filling in the seconds between preview and Confirm turns an approved
    "sell 10.00 shares" into a sale of 15.00. `expected_shares` refuses that:
    the rebuilt plan may shrink, but it may not grow past what was approved.
    `_unconfirmed` returns the correct arguments in `confirm_args`.
    """
    with _session(client) as (api, settings):
        market = _resolve_market(api, market_ref)
        plan = trading.build_sell_plan(
            api,
            settings,
            market=market,
            outcome=outcome,
            shares=shares,
            fraction=fraction,
            limit_price=limit_price,
        )
        if plan.blockers:
            return _refused("sell", plan)
        if not confirm:
            return _unconfirmed("sell", plan)
        drifted = _guard_confirmed("sell", plan, expected_shares)
        if drifted is not None:
            return drifted

        collector = CollectingNotifier()
        result = trading.execute_plan(api, settings, plan, notifier=collector)
    return _executed("sell", result, _events(collector))


@_safe
def cancel_orders(*, client: SecureClient | None = None) -> dict:
    """Cancel every resting order. Costs nothing, but it also removes any
    take-profit limit orders that were working while the bot was offline."""
    with _session(client) as (api, _settings):
        outcome = trading.cancel_all_orders(api)

    # `outcome` carries its own ok/error keys. Never splat it into the envelope
    # helpers: pass what we want through by name so the exchange's own report
    # can never overwrite - or collide with - this response's ok/error.
    canceled = [str(order_id) for order_id in (outcome.get("canceled") or [])]
    count = int(outcome.get("canceled_count") or len(canceled))
    not_canceled = dict(outcome.get("not_canceled") or {})
    error = outcome.get("error")

    if error:
        # The call itself failed. This is the exchange's own reason, and it is
        # the whole value of the response - report it verbatim.
        return _fail(
            "cancel_orders",
            str(error),
            text=f"Cancel failed - nothing was cancelled: {error}",
            canceled=canceled,
            canceled_count=count,
            not_canceled=not_canceled,
        )

    lines = [f"Cancelled {count} resting order(s)."]
    if count:
        lines.append("  Any take-profit that was resting on the book is gone with them.")
    if not not_canceled:
        return _ok(
            "cancel_orders",
            "\n".join(lines),
            canceled=canceled,
            canceled_count=count,
            not_canceled={},
        )

    # Partial cancel: some orders are still working. That is not success - a
    # caller branching on `ok` must not be told "all clear" while an order is
    # still live on the book - so it fails with a reason that names the
    # survivors, and the text still says what *did* get cancelled.
    detail = "; ".join(f"{oid}: {why}" for oid, why in sorted(not_canceled.items()))
    lines.append(f"  {len(not_canceled)} order(s) are STILL RESTING and were not cancelled: {detail}")
    return _fail(
        "cancel_orders",
        f"{len(not_canceled)} of {count + len(not_canceled)} order(s) could not be cancelled: {detail}",
        text="\n".join(lines),
        canceled=canceled,
        canceled_count=count,
        not_canceled=not_canceled,
    )


@_safe
def redeem(*, client: SecureClient | None = None) -> dict:
    """Claim every settled position.

    A position that lost is redeemable and pays $0 - redeeming it clears the
    list, it does not recover the loss. The response says which is which.
    """
    collector = CollectingNotifier()
    with _session(client) as (api, _settings):
        pending = portfolio.get_redeemable(api)
        if not pending:
            return _ok("redeem", "Nothing to redeem - no settled positions.", redeemed=[], count=0, total_usdc=0.0, events=[])
        results = portfolio.redeem_all(api, notifier=collector)

    claimed = [r for r in results if r.get("ok")]
    total = round(sum(float(r.get("expected_usdc") or 0.0) for r in claimed), 6)
    failed = [r for r in results if not r.get("ok")]

    lines = [f"Redeemed {len(claimed)} of {len(results)} settled market(s), paying {_usd(total)}."]
    if total < 0.01 and claimed:
        lines.append("  Those positions resolved against you: redeeming pays $0 and only clears them from the list.")
    for entry in failed:
        lines.append(f"  FAILED {_clip(str(entry.get('title') or ''), 50)}: {entry.get('error')}")
    events = _events(collector)
    if events:
        lines.append(_events_text(events))

    return _ok(
        "redeem",
        "\n".join(lines),
        redeemed=results,
        count=len(claimed),
        failed=len(failed),
        total_usdc=total,
        events=events,
    )


# --------------------------------------------------------------------------
# exit rules
# --------------------------------------------------------------------------
def _enforcement(kind: str) -> str:
    if kind == "take_profit":
        return (
            "Enforced by the monitor. Polymarket can hold a take-profit as a resting SELL "
            "limit order that survives the bot being offline - this rule is not that; it "
            "only fires while the monitor is running."
        )
    return (
        "Enforced by the monitor only. Polymarket has no native stop-loss or time exit, so "
        "this protects nothing while the monitor is not running."
    )


def _trigger_text(rule: ExitRule) -> str:
    if rule.kind == "take_profit" and rule.target_price is not None:
        pct = f" ({rule.target_pct:+.1f}% vs entry)" if rule.target_pct is not None else ""
        return f"sell when price >= {rule.target_price:.4f}{pct}"
    if rule.kind == "stop_loss" and rule.target_price is not None:
        pct = f" ({rule.target_pct:+.1f}% vs entry)" if rule.target_pct is not None else ""
        return f"sell when price <= {rule.target_price:.4f}{pct}"
    if rule.kind == "trailing_stop" and rule.trail_pct is not None:
        hwm = f" (high so far {rule.high_water_mark:.4f})" if rule.high_water_mark is not None else ""
        return f"sell when price falls {rule.trail_pct:g}% below the highest price seen{hwm}"
    if rule.kind == "time_exit" and rule.expires_at:
        return f"sell at {rule.expires_at}"
    return f"{rule.kind} (incomplete - it cannot fire as stored)"


def _exit_shares(position: portfolio.PositionView, fraction: float) -> float:
    """Shares the rule would actually offer, on the exchange's 0.01-share grid."""
    return math.floor(position.shares * max(0.0, min(fraction, 1.0)) * 100.0) / 100.0


def _market_min_size(market: Market) -> float | None:
    try:
        value = market.trading.minimum_order_size
    except Exception:
        return None
    return None if value is None else float(value)


def _parse_iso(value: str | None) -> datetime | None:
    """ISO-8601 (with or without a trailing Z) -> aware datetime, or None."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _rule_warnings(market: Market, position: portfolio.PositionView, rule: ExitRule) -> list[str]:
    """Say out loud when a stored rule is unlikely to do what it looks like.

    Warnings only - storing the rule is still the right call, because the thing
    a warning describes (a thin holding, a halted market, a target the price is
    already past) can change before the monitor's next pass. Nothing here is
    about `max_order_usdc`: that cap applies to BUYs only, and a sell-side cap
    warning would be describing a limit that no longer exists.
    """
    notes: list[str] = []

    exit_shares = _exit_shares(position, rule.exit_fraction)
    if exit_shares <= 0:
        notes.append(
            f"{rule.exit_fraction:g} of {position.shares:,.4f} shares rounds to 0 on the "
            "exchange's 0.01-share grid - as stored, this rule can never build an order."
        )
    else:
        minimum = _market_min_size(market)
        if minimum and exit_shares < minimum:
            notes.append(
                f"The exit would be {exit_shares:,.2f} shares, under this market's "
                f"{minimum:g}-share minimum order size - the exchange would reject it unless "
                "the holding grows first."
            )

    if not is_tradable(market):
        notes.append(
            "This market is not accepting orders right now, so no exit could be sent while "
            "that lasts."
        )

    if position.redeemable:
        notes.append(
            "The exchange already flags this position redeemable - a settled position is "
            "redeemed, not sold, and an exit rule never fires on one."
        )

    # Would it fire on the very next pass? That is legal and sometimes intended
    # (arming a trailing stop on a position already off its high), but nobody
    # should learn it from the sell order.
    try:
        decision = evaluate_rule(rule, position, position.cur_price)
    except Exception:
        decision = None
    if decision is not None and decision.should_exit:
        notes.append(
            f"This already qualifies at the current price {position.cur_price:.4f} - "
            f"the next monitor pass will act on it. ({decision.reason})"
        )

    # A time exit past the market's own resolution never fires: the position
    # settles first, and a settled position is redeemed rather than sold.
    if rule.kind == "time_exit":
        deadline = _parse_iso(rule.expires_at)
        end_date = market.state.end_date
        if deadline is not None and end_date is not None:
            end = end_date if end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
            if deadline >= end:
                notes.append(
                    f"The market resolves at {end.isoformat(timespec='seconds')}, at or before "
                    f"this exit at {deadline.isoformat(timespec='seconds')} - the position settles "
                    "first, so this rule would never fire. Redemption is the exit instead."
                )

    return notes


def _rule_line(rule: ExitRule) -> str:
    state = "" if rule.active else " [INACTIVE]"
    portion = "" if rule.exit_fraction >= 1.0 else f", exiting {rule.exit_fraction * 100:.0f}% of the holding"
    return (
        f"  {rule.id}  {rule.kind:<13}{state} {_clip(rule.market_title, 44)} [{rule.outcome}]\n"
        f"         {_trigger_text(rule)}{portion}"
    )


@_safe
def set_rule(
    market_ref: str,
    outcome: str,
    kind: str,
    *,
    target_price: float | None = None,
    target_pct: float | None = None,
    trail_pct: float | None = None,
    exit_fraction: float = 1.0,
    expires_at: str | None = None,
    note: str | None = None,
    client: SecureClient | None = None,
) -> dict:
    """Attach an exit rule to a position you actually hold.

    `expires_at` is not in the CLI-facing contract line but time_exit is
    unusable without it, so it is accepted here as an optional extra.

    Anything that makes the rule *unlikely to do what it looks like* - an exit
    size the exchange would reject, a halted market, a trigger the price has
    already passed, a time exit set after the market resolves - is reported in
    `warnings` and shown in the text. The rule is still stored: those facts can
    all change before the monitor's next pass, and a stored rule the owner can
    see beats a silent refusal. Only the cases where nothing could ever be sold
    (no position, or a settled one) refuse outright.
    """
    if kind not in VALID_KINDS:
        return _fail(
            "set_rule",
            f"Unknown rule kind {kind!r}; expected one of {', '.join(VALID_KINDS)}.",
        )

    with _session(client) as (api, settings):
        market = _resolve_market(api, market_ref)
        target = _outcome_of(market, outcome)
        token_id = str(target.token_id or "")
        if not token_id:
            return _fail("set_rule", f"Market has no tradable '{target.label}' token.")

        # Live read: a rule can only ever sell what is actually held right now.
        position = portfolio.find_position(api, token_id=token_id)
        if position is None or position.shares <= 0:
            return _fail(
                "set_rule",
                f"You hold no '{target.label}' shares in {_title(market)!r} - "
                "there is nothing for a rule to sell.",
            )
        if position.is_resolved:
            # Not a warning: this one can never become true again. The monitor
            # refuses to fire on a settled position and make_rule rejects it too.
            return _fail(
                "set_rule",
                f"{_title(market)!r} has already settled - a settled position is redeemed, not "
                f"sold, so no exit rule could ever fire on it. Redeem it instead "
                f"(it pays {_usd(position.current_value)}).",
                position=position.to_dict(),
            )

        try:
            rule = make_rule(
                kind=kind,  # type: ignore[arg-type]
                position=position,
                target_price=target_price,
                target_pct=target_pct,
                trail_pct=trail_pct,
                exit_fraction=exit_fraction,
                expires_at=expires_at,
                note=note,
            )
        except ValueError as exc:
            # make_rule's messages are already written for a human; don't bury
            # them behind an exception type prefix.
            return _fail("set_rule", str(exc))

        warnings = _rule_warnings(market, position, rule)
        stored = RuleStore(settings).add(rule)

    distance = ""
    if stored.target_price and position.cur_price > 0:
        move = (stored.target_price / position.cur_price - 1) * 100.0
        distance = f" (target {stored.target_price:.4f} is {move:+.1f}% away)"

    text = "\n".join(
        [
            f"RULE SET - {stored.kind} #{stored.id}",
            f"  Market   : {_clip(stored.market_title, 60)}",
            f"  Outcome  : '{stored.outcome}' - holding {position.shares:,.2f} shares @ {position.avg_price:.4f}",
            f"  Price now: {position.cur_price:.4f}{distance}",
            f"  Trigger  : {_trigger_text(stored)}",
            f"  Exit     : {stored.exit_fraction * 100:.0f}% of the holding "
            f"({_exit_shares(position, stored.exit_fraction):,.2f} shares at today's size)",
            *(f"  !        : {note_}" for note_ in warnings),
            f"  Warning  : {_enforcement(stored.kind)}",
        ]
    )
    return _ok(
        "set_rule",
        text,
        rule=stored.to_dict(),
        position=position.to_dict(),
        warnings=warnings,
        enforcement=_enforcement(stored.kind),
    )


@_safe
def list_rules(*, active_only: bool = False) -> dict:
    """Stored exit rules. Local state only - no network call."""
    settings = load_settings()
    rules = RuleStore(settings).list(active_only=active_only)

    if not rules:
        return _ok(
            "list_rules",
            "No exit rules stored." if not active_only else "No active exit rules.",
            rules=[],
            count=0,
            active_only=active_only,
            monitor_dry_run=settings.monitor_dry_run,
        )

    lines = [f"EXIT RULES ({len(rules)}{' active' if active_only else ''})"]
    lines.extend(_rule_line(rule) for rule in rules)
    lines.append("")
    lines.append(
        "  Monitor is in DRY RUN - rules are evaluated and reported, never executed."
        if settings.monitor_dry_run
        else "  Monitor is LIVE - a triggered rule will send a sell order."
    )
    lines.append("  None of these exist on Polymarket; they fire only while the monitor runs.")

    return _ok(
        "list_rules",
        "\n".join(lines),
        rules=[r.to_dict() for r in rules],
        count=len(rules),
        active_only=active_only,
        monitor_dry_run=settings.monitor_dry_run,
    )


@_safe
def remove_rule(rule_id: str) -> dict:
    """Delete one stored rule. Local state only - no network call."""
    settings = load_settings()
    store = RuleStore(settings)
    existing = store.get(rule_id)
    if existing is None:
        return _fail("remove_rule", f"No rule with id {rule_id!r}.", rule_id=rule_id, removed=False)
    if not store.remove(rule_id):
        return _fail("remove_rule", f"Rule {rule_id!r} could not be removed.", rule_id=rule_id, removed=False)
    return _ok(
        "remove_rule",
        f"Removed {existing.kind} rule #{existing.id} on '{_clip(existing.market_title, 50)}' [{existing.outcome}].",
        rule_id=rule_id,
        removed=True,
        rule=existing.to_dict(),
    )


#: Names the monitor may use for "this sweep could not read live state at all".
#: A sweep that merely collected per-rule errors is still a successful sweep;
#: this is the hard failure - no positions, or no rule store, so nothing was
#: evaluated. Checked on the report first, then on the Monitor itself, because
#: the flag lives on the instance today and is moving onto MonitorReport.
_HARD_FAILURE_FIELDS = (
    "sweep_failed",
    "hard_failure",
    "read_failed",
    "state_read_failed",
    "live_read_failed",
    "failed",
)


def _sweep_failed(monitor: Any, report: Any) -> bool:
    """Did this sweep fail to read live state? See `_HARD_FAILURE_FIELDS`."""
    data = report.to_dict() if hasattr(report, "to_dict") else {}
    for name in _HARD_FAILURE_FIELDS:
        for value in (getattr(report, name, None), data.get(name)):
            if isinstance(value, bool):
                return value
    value = getattr(monitor, "_last_run_failed", None)
    return value if isinstance(value, bool) else False


@_safe
def monitor_once(*, client: SecureClient | None = None) -> dict:
    """One monitoring pass: re-read positions, evaluate rules, act if live.

    `ok` is False when the sweep could not read live state - a data-api outage,
    an unreadable rule store. `Monitor.run_once` deliberately swallows those so
    one bad market cannot end a sweep, which used to make this function return
    `ok=True` forever: a caller's failure counter never moved and its backoff
    was unreachable during a total outage. A pass that ran and merely collected
    per-rule errors is still `ok=True` - it did its job.
    """
    try:
        from polymarket_bot.monitor import Monitor
    except ImportError as exc:
        # Imported here rather than at module scope so the rest of the facade
        # stays usable even if the monitor module is missing or broken.
        return _fail("monitor_once", f"Monitor unavailable ({type(exc).__name__}: {exc}).")

    collector = CollectingNotifier()
    with _session(client) as (api, settings):
        monitor = Monitor(api, settings, notifier=collector, store=RuleStore(settings))
        report = monitor.run_once()
        failed = _sweep_failed(monitor, report)

    events = _events(collector)
    data = report.to_dict()
    text = report.to_text()
    if events:
        text = f"{text}\n{_events_text(events)}"

    if failed:
        errors = list(data.get("errors") or [])
        reason = errors[0] if errors else "the sweep could not read live state."
        return _fail(
            "monitor_once",
            reason,
            text=f"{text}\n  SWEEP FAILED - live state could not be read, so NO rule was checked.",
            report=data,
            events=events,
            dry_run=data.get("dry_run"),
            sweep_failed=True,
        )

    return _ok(
        "monitor_once",
        text,
        report=data,
        events=events,
        dry_run=data.get("dry_run"),
        sweep_failed=False,
    )
