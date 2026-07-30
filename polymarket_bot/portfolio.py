"""Account state: cash, positions, P&L and redemptions.

Read-only except for `redeem_all`, which is the single write path in this
module (and even that only claims already-settled payouts — it can never open
or close a trade).

Two Polymarket facts shape everything here:

  1. `get_balance_allowance(asset_type="COLLATERAL").balance` is an **int in
     6-decimal fixed point**, not dollars. 21826697 means $21.826697.
  2. Positions do not disappear when a market resolves. A market you lost
     stays in the list forever with `cur_price == 0`, `percent_pnl ≈ -100`
     and `redeemable == True`. Those are dead weight, not holdings, so the
     default position view hides them (`include_resolved=False`) and they are
     surfaced separately as `redeemable_*`.

  3. The data-api hides small holdings unless you ask for them. Its
     `sizeThreshold` parameter defaults to **1**, so a position of 0.99 shares
     simply is not in the response. Every read here passes `size_threshold=0`:
     a partially-sold position must not look sold, and a 0.9-share winner is
     still real money to redeem.

Redeeming a *lost* position pays $0. It clears clutter, nothing else. Nothing
in this module should ever be phrased as if redeeming recovers money.

The data-api omits fields freely, so every `Position` attribute is treated as
possibly-None and derived from whatever else is available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from polymarket import Position, SecureClient

from polymarket_bot.notify import Level, Notifier

# Positions come back paginated; 100 keeps a full account to one or two calls.
_POSITION_PAGE_SIZE = 100

# The data-api's own default is sizeThreshold=1, which silently drops every
# holding under one share. Ask for everything and filter here, where the rule
# is visible. `analytics` does the same, so the two modules agree on what the
# account holds.
_POSITION_SIZE_THRESHOLD = 0

# A resolved market prices at exactly 0 or 1, but the data-api rounds, so
# compare with a tolerance rather than `in {0, 1}`.
_RESOLVED_EPS = 1e-6


def _f(value: Decimal | float | int | None, default: float = 0.0) -> float:
    """Decimal/None -> float. The API omits numeric fields rather than sending 0."""
    if value is None:
        return default
    return float(value)


def _money(value: float) -> float:
    # USDC is 6dp on-chain; rounding there kills float noise without losing a cent.
    # float() because `sum([])` is an int and money is a float at every boundary.
    return round(float(value), 6)


@dataclass
class PositionView:
    """One outcome-token holding, with everything a caller needs pre-derived.

    `unrealized_pnl` is only genuinely unrealized while `is_resolved` is False.
    On a settled row it is the final, decided P&L that redemption will book —
    `PortfolioSummary` keeps the two apart for exactly that reason.
    """

    condition_id: str
    token_id: str
    opposite_token_id: str | None
    market_title: str
    slug: str | None
    outcome: str  # "Yes"/"No", or the market's own label ("Up"/"Down", ...)
    shares: float
    avg_price: float
    cur_price: float
    cost_basis: float
    current_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    realized_pnl: float
    redeemable: bool
    is_resolved: bool  # cur_price at 0 or 1 AND redeemable => market has settled
    end_date: str | None

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "opposite_token_id": self.opposite_token_id,
            "market_title": self.market_title,
            "slug": self.slug,
            "outcome": self.outcome,
            "shares": self.shares,
            "avg_price": self.avg_price,
            "cur_price": self.cur_price,
            "cost_basis": self.cost_basis,
            "current_value": self.current_value,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "realized_pnl": self.realized_pnl,
            "redeemable": self.redeemable,
            "is_resolved": self.is_resolved,
            "end_date": self.end_date,
        }


@dataclass
class PortfolioSummary:
    """Whole-account snapshot.

    The account splits cleanly in two, and the field names say which half they
    describe:

      * **live** holdings - still tradable, the price can still move.
        `open_positions` counts them, `open_positions_value` prices them,
        `unrealized_pnl` is their P&L, and they are the rows in `positions`.
      * **settled** holdings - the market resolved, the outcome is decided,
        only redemption is left. `settled_positions` counts them,
        `settled_value` prices them, `settled_pnl` is their P&L.

    `settled_pnl` is deliberately *not* called unrealized. Nothing about it can
    move any more: it is a win waiting to be claimed or a loss already taken,
    and redeeming simply shifts `settled_value` into `cash_usdc`. Reporting it
    inside `unrealized_pnl` is what produced headlines like
    "open=0, unrealized=-13.93", which reads as a contradiction and invites the
    owner to wait for a recovery that cannot happen.

    The two halves partition every held position exactly once, so:

        positions_value = open_positions_value + settled_value
        total_pnl       = unrealized_pnl       + settled_pnl
        total_value     = cash_usdc            + positions_value

    and nothing is double counted. `total_pnl` is kept so a caller that wants
    the whole-account number does not have to re-derive it (it is what the old
    `unrealized_pnl` used to hold).

    `redeemable_count` / `redeemable_value` are a *different cut* of the same
    rows - whatever the exchange currently flags claimable, a superset of the
    settled ones - so they are not part of that partition and must not be
    added to it.
    """

    cash_usdc: float
    positions_value: float  # every holding, live and settled
    total_value: float
    open_positions: int
    unrealized_pnl: float  # live positions only
    realized_pnl: float
    redeemable_count: int
    redeemable_value: float
    positions: list[PositionView] = field(default_factory=list)  # live only
    # Appended rather than interleaved so existing positional/keyword
    # construction of the fields above keeps working unchanged.
    open_positions_value: float = 0.0
    settled_positions: int = 0
    settled_value: float = 0.0
    settled_pnl: float = 0.0
    total_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cash_usdc": self.cash_usdc,
            "positions_value": self.positions_value,
            "open_positions_value": self.open_positions_value,
            "settled_value": self.settled_value,
            "total_value": self.total_value,
            "open_positions": self.open_positions,
            "settled_positions": self.settled_positions,
            "unrealized_pnl": self.unrealized_pnl,
            "settled_pnl": self.settled_pnl,
            "total_pnl": self.total_pnl,
            "realized_pnl": self.realized_pnl,
            "redeemable_count": self.redeemable_count,
            "redeemable_value": self.redeemable_value,
            "positions": [p.to_dict() for p in self.positions],
        }


def _outcome_label(position: Position) -> str:
    if position.outcome:
        return str(position.outcome)
    # Fall back to the index: Polymarket orders binary outcomes Yes(0)/No(1).
    if position.outcome_index == 0:
        return "Yes"
    if position.outcome_index == 1:
        return "No"
    return ""


def _to_view(position: Position) -> PositionView:
    shares = _f(position.size)
    avg_price = _f(position.avg_price)
    cur_price = _f(position.cur_price)

    # initial_value/current_value are usually present; recompute when they aren't.
    cost_basis = _f(position.initial_value, shares * avg_price)
    current_value = _f(position.current_value, shares * cur_price)

    # For a few seconds after a fill the data API returns the position with
    # avgPrice/initialValue still zero. Rendering that literally invents a
    # 100% profit (cost $0, value $3 -> "+$3.00"), and any percentage-based
    # rule computes its target from a zero entry: take_profit at +50% becomes
    # 0 * 1.5 = 0, the live price clears it instantly, and the monitor sells a
    # position bought seconds earlier. Treat an unknown cost basis as unknown.
    cost_known = shares <= 0 or (avg_price > 0 and cost_basis > 0)

    unrealized = _f(position.cash_pnl, current_value - cost_basis)
    if position.percent_pnl is not None:
        unrealized_pct = float(position.percent_pnl)
    else:
        unrealized_pct = (unrealized / cost_basis * 100.0) if cost_basis else 0.0
    if not cost_known:
        # No entry price yet: report no P&L rather than a fabricated one.
        unrealized, unrealized_pct = 0.0, 0.0

    redeemable = bool(position.redeemable)
    settled_price = cur_price <= _RESOLVED_EPS or cur_price >= 1.0 - _RESOLVED_EPS

    return PositionView(
        condition_id=str(position.condition_id),
        token_id=str(position.token_id or ""),
        opposite_token_id=str(position.opposite_token_id) if position.opposite_token_id else None,
        market_title=position.title or position.slug or str(position.condition_id),
        slug=position.slug or None,
        outcome=_outcome_label(position),
        shares=_money(shares),
        avg_price=round(avg_price, 6),
        cur_price=round(cur_price, 6),
        cost_basis=_money(cost_basis),
        current_value=_money(current_value),
        unrealized_pnl=_money(unrealized),
        unrealized_pnl_pct=round(unrealized_pct, 4),
        realized_pnl=_money(_f(position.realized_pnl)),
        redeemable=redeemable,
        is_resolved=redeemable and settled_price,
        end_date=position.end_date.isoformat() if position.end_date else None,
    )


def _all_views(client: SecureClient) -> list[PositionView]:
    """Every position on the account, live and settled, biggest value first."""
    views: list[PositionView] = []
    paginator = client.list_positions(
        size_threshold=_POSITION_SIZE_THRESHOLD,
        page_size=_POSITION_PAGE_SIZE,
    )
    for position in paginator.iter_items():
        view = _to_view(position)
        # Fully-exited markets can linger with size 0; they aren't holdings.
        # (This is what `size_threshold=0` lets through, and all it lets through.)
        if view.shares <= 0:
            continue
        views.append(view)
    views.sort(key=lambda v: (v.current_value, v.cost_basis), reverse=True)
    return views


def get_cash_balance(client: SecureClient) -> float:
    """Free USDC collateral in dollars (the SDK returns 6-decimal fixed point)."""
    balance = client.get_balance_allowance(asset_type="COLLATERAL")
    return _money(balance.balance / 1_000_000)


def get_positions(client: SecureClient, *, include_resolved: bool = False) -> list[PositionView]:
    """Current holdings.

    By default this is the *tradable* book: settled markets (resolved winners
    and losers alike) are filtered out, because they can only be redeemed, not
    traded. Pass `include_resolved=True` to see everything.
    """
    views = _all_views(client)
    if include_resolved:
        return views
    return [v for v in views if not v.is_resolved]


def get_portfolio_summary(client: SecureClient) -> PortfolioSummary:
    """Cash + positions in one read. See PortfolioSummary for what spans what."""
    cash = get_cash_balance(client)
    views = _all_views(client)
    # `is_resolved` partitions the book: a holding is either still tradable or
    # settled and waiting to be redeemed. Every total below is built from one
    # side or the other, never from an overlapping mix.
    open_views = [v for v in views if not v.is_resolved]
    settled_views = [v for v in views if v.is_resolved]
    redeemable = [v for v in views if v.redeemable]

    open_value = _money(sum(v.current_value for v in open_views))
    settled_value = _money(sum(v.current_value for v in settled_views))
    positions_value = _money(open_value + settled_value)
    unrealized = _money(sum(v.unrealized_pnl for v in open_views))
    settled_pnl = _money(sum(v.unrealized_pnl for v in settled_views))

    return PortfolioSummary(
        cash_usdc=cash,
        positions_value=positions_value,
        total_value=_money(cash + positions_value),
        open_positions=len(open_views),
        unrealized_pnl=unrealized,
        realized_pnl=_money(sum(v.realized_pnl for v in views)),
        redeemable_count=len(redeemable),
        redeemable_value=_money(sum(v.current_value for v in redeemable)),
        positions=open_views,
        open_positions_value=open_value,
        settled_positions=len(settled_views),
        settled_value=settled_value,
        settled_pnl=settled_pnl,
        total_pnl=_money(unrealized + settled_pnl),
    )


def find_position(
    client: SecureClient,
    *,
    token_id: str | None = None,
    condition_id: str | None = None,
    outcome: str | None = None,
) -> PositionView | None:
    """Look up a single holding. Searches settled positions too.

    Callers act on the result (sell, set a rule, redeem), so this always
    re-reads live state — never trust a cached position.
    """
    if token_id is None and condition_id is None and outcome is None:
        raise ValueError("find_position needs at least one of token_id / condition_id / outcome")

    wanted_condition = condition_id.lower() if condition_id else None
    wanted_outcome = outcome.strip().lower() if outcome else None

    for view in _all_views(client):
        if token_id is not None and view.token_id != str(token_id):
            continue
        if wanted_condition is not None and view.condition_id.lower() != wanted_condition:
            continue
        if wanted_outcome is not None and view.outcome.lower() != wanted_outcome:
            continue
        return view
    return None


def get_redeemable(client: SecureClient) -> list[PositionView]:
    """Positions whose market has settled and whose payout can be claimed.

    A losing position is redeemable too and pays $0 — check `current_value`
    before treating this list as money waiting to be collected.
    """
    return [v for v in _all_views(client) if v.redeemable]


def redeem_all(client: SecureClient, notifier: Notifier | None = None) -> list[dict]:
    """Claim every redeemable position. WRITE OPERATION — sends transactions.

    Redemption pays out `current_value`, which is **$0 for a position that
    lost** (`cur_price == 0`). For those, this is housekeeping that clears the
    portfolio list; it does not recover any of the loss. Each result carries
    `expected_usdc` so the caller can say so honestly.

    Returns one dict per condition_id: {condition_id, title, ok, error,
    expected_usdc, transaction_hash}.
    """
    results: list[dict] = []

    # One redemption call settles a whole condition, so collapse the yes/no
    # legs of the same market into a single transaction.
    by_condition: dict[str, list[PositionView]] = {}
    for view in get_redeemable(client):
        by_condition.setdefault(view.condition_id, []).append(view)

    for condition_id, views in by_condition.items():
        title = views[0].market_title
        expected = _money(sum(v.current_value for v in views))
        entry: dict = {
            "condition_id": condition_id,
            "title": title,
            "ok": False,
            "error": None,
            "expected_usdc": expected,
            "transaction_hash": None,
        }
        try:
            handle = client.redeem_positions(condition_id=condition_id)
            outcome = handle.wait()
            entry["ok"] = True
            entry["transaction_hash"] = str(outcome.transaction_hash) if outcome.transaction_hash else None
        except Exception as exc:  # one bad market must not abort the sweep
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)

        # Notify outside the try: a broken sink must never make a redemption
        # that actually landed look like a failure.
        if notifier is not None:
            message: str
            level: Level
            if entry["ok"]:
                worth = (
                    f"pays ${expected:.2f}"
                    if expected > 0
                    else "pays $0 - this position lost; redeeming only clears it from the list"
                )
                message, level = f"Redeemed '{title}' ({worth}).", "trade"
            else:
                message, level = f"Redeem failed for '{title}': {entry['error']}", "error"
            try:
                notifier.send(message, level=level)
            except Exception:
                pass

    return results
