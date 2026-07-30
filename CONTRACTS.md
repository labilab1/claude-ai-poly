# Internal module contracts

Authoritative spec for `polymarket_bot`. Every module must match these
signatures exactly so the layers compose. **This file is the source of truth —
if code and this file disagree, the code is wrong.**

## Ground rules

1. **Core modules never print and never call `input()`.** They return data or
   emit through a `Notifier`. Presentation lives in `scripts/` (and later a
   Telegram bot). This is what makes the system driveable from Telegram.
2. **Every returned dataclass has `to_dict() -> dict`** producing only
   JSON-safe primitives (`str`, `int`, `float`, `bool`, `None`, `list`, `dict`).
   Convert `Decimal` → `float`, `datetime`/`date` → ISO `str`.
3. **Money is USDC as `float`** at module boundaries. Shares are `float`.
   Prices are `float` in `0..1`.
4. **Never trust cached position state.** Positions change outside the bot
   (the owner can trade on the website). Always re-read live state before
   acting on it.
5. Type hints everywhere. `from __future__ import annotations` at the top.
6. Only depend on: stdlib, `polymarket` (the SDK), `polymarket_bot.*`,
   and `requests` (already used by `wallet_status`). No new pip deps.

## Environment

- Windows. Python 3.14. Run things as:
  `cd d:\POLYMARKET\claude-ai-poly; .\venv\Scripts\Activate.ps1; python -m ...`
- Live account has real money (~$21). **Never place a real order while
  building.** Dry-run/preview paths only. The one exception is when the repo
  owner explicitly asks for a specific trade.

## SDK facts (verified against installed `polymarket-client` 0.2.0)

`client` below is a `polymarket.SecureClient` from `polymarket_bot.client.get_client()`.
`SecureClient` also exposes all read methods — no separate PublicClient needed.

```python
# --- market data ---
client.list_markets(closed=False, condition_ids=..., slug=..., page_size=20)  # -> Paginator[Market]; .iter_items() / .first_page().items
client.get_market(slug=...) | client.get_market(id=...) | client.get_market(url=...)  # -> Market
client.get_order_book(token_id=...)        # -> OrderBook
client.get_spreads(token_ids=[...])        # -> dict[token_id, Decimal]
client.get_midpoint(token_id=...)          # -> Decimal
client.get_price(token_id=..., side="BUY"|"SELL")   # -> Decimal
client.get_last_trade_price(token_id=...)  # -> LastTradePrice(.price, .side)
client.get_price_history(token_id=..., interval="max"|"1w"|"1d"|"6h"|"1h", fidelity=..., start_ts=..., end_ts=...)  # -> tuple[PriceHistoryPoint(.t, .p), ...]
client.estimate_market_price(token_id=..., side="BUY", amount=<usdc>, order_type="FAK")   # -> Decimal
client.estimate_market_price(token_id=..., side="SELL", shares=<shares>, order_type="FAK")  # -> Decimal

# --- account ---
client.get_balance_allowance(asset_type="COLLATERAL")            # -> BalanceAllowance(.balance int 6dp, .allowances dict)
client.get_balance_allowance(asset_type="CONDITIONAL", token_id=...)  # -> same, .balance = raw shares 6dp
client.list_positions(market=[condition_id], size_threshold=..., redeemable=..., page_size=...)  # -> Paginator[Position]
client.list_trades(market=[condition_id], page_size=...)         # -> Paginator[Trade]   (data-api view)
client.list_account_trades(market=condition_id)                  # -> Paginator[ClobTrade] (authoritative for our orders)
client.list_open_orders(token_id=..., market=...)                # -> Paginator[OpenOrder]
client.get_portfolio_values()                                    # -> tuple[PortfolioValue(.user, .value)]

# --- trading ---
client.place_market_order(token_id=..., side="BUY",  amount=<usdc>,   max_price=..., order_type="FAK")   # -> OrderResponse
client.place_market_order(token_id=..., side="SELL", shares=<shares>, min_price=..., order_type="FAK")   # -> OrderResponse
client.place_limit_order(token_id=..., price=<0..1>, size=<shares>, side="BUY"|"SELL", post_only=False, expiration=None)  # -> OrderResponse
client.cancel_order(order_id=...) / client.cancel_orders(order_ids=[...]) / client.cancel_all()
client.redeem_positions(condition_id=...)   # -> SyncTransactionHandle (.wait())
```

Model fields actually used:

```python
Market: .id .slug .condition_id .question .description
        .state(.active .closed .accepting_orders .end_date)
        .outcomes.yes/.no -> MarketOutcome(.label .token_id .price)
        .metrics(.volume_24hr .liquidity_num)
        .prices(.best_bid .best_ask .spread .one_day_price_change)
        .trading(.minimum_order_size .minimum_tick_size)
        .rewards(.clob_rewards[].rewards_daily_rate .rewards_min_size)
        .tags[](.label)

Position: .condition_id .token_id .opposite_token_id .size .avg_price .cur_price
          .initial_value .current_value .cash_pnl .percent_pnl .realized_pnl
          .redeemable .mergeable .title .slug .outcome .outcome_index .end_date

OrderBook: .bids ASC (best bid = bids[-1]), .asks DESC (best ask = asks[-1]);
           levels have .price .size (Decimal). Also .min_order_size .tick_size .neg_risk

OrderResponse: .ok .order_id .status ("live"|"matched"|"delayed") .making_amount .taking_amount
               .trade_ids .transactions_hashes  (may also carry .error_msg)

ClobTrade: .side .size .price .status .outcome .transaction_hash .trader_side .match_time
```

Gotchas already hit — do not re-learn these the hard way:
- `AssetType` is a plain `Literal` string: pass `"COLLATERAL"`, not an enum.
- `get_balance_allowance().balance` is an **int in 6-decimal fixed point**;
  divide by 1_000_000 for USDC. Same for CONDITIONAL (shares).
- Polymarket has **no native stop-loss**. Stop-loss/trailing require our own
  polling monitor. Take-profit CAN be a resting SELL limit order (survives the
  bot being offline) — support both and say which is which.
- A market minimum order size is often 5 shares; a $1 order only clears it on
  low-priced outcomes. Always surface this before it gets rejected.

## Existing modules (already written — do not rewrite)

- `polymarket_bot/config.py` → `Settings` (frozen dataclass), `load_settings()`.
  Fields: `private_key, wallet, max_order_usdc, max_position_usdc,
  daily_loss_limit_usdc, min_cash_reserve_usdc, monitor_interval_seconds,
  monitor_dry_run, data_dir, telegram_bot_token, telegram_chat_id`,
  properties `rules_path`, `state_path`, method `ensure_data_dir()`.
- `polymarket_bot/notify.py` → `Notifier` Protocol with
  `send(message: str, *, level: Level="info")`; `NullNotifier`,
  `ConsoleNotifier`, `CollectingNotifier`, `MultiNotifier`.
  `Level = Literal["debug","info","trade","alert","error"]`.
- `polymarket_bot/client.py` → `get_client() -> SecureClient`.
- `polymarket_bot/markets.py` → `is_tradable(market)`, `daily_reward_rate(market)`,
  `BookSnapshot`, `list_tradable_markets`, `get_market_by_condition_id`,
  `get_market_by_slug`, `get_book_snapshot`, `get_spreads`.
  (May be extended, but keep these names working.)
- `polymarket_bot/advisor.py` → `DISCLAIMER`, `implied_probability_note`,
  `liquidity_note`, `time_to_resolution`, `Briefing`, `briefing(market, snapshot)`.

## Modules to build

### `polymarket_bot/portfolio.py`
```python
@dataclass
class PositionView:
    condition_id: str; token_id: str; opposite_token_id: str | None
    market_title: str; slug: str | None; outcome: str          # "Yes"/"No"
    shares: float; avg_price: float; cur_price: float
    cost_basis: float; current_value: float
    unrealized_pnl: float; unrealized_pnl_pct: float
    realized_pnl: float
    redeemable: bool; is_resolved: bool          # is_resolved: cur_price in {0,1} and redeemable
    end_date: str | None
    def to_dict(self) -> dict: ...

@dataclass
class PortfolioSummary:
    cash_usdc: float; positions_value: float; total_value: float
    open_positions: int; unrealized_pnl: float; realized_pnl: float
    redeemable_count: int; redeemable_value: float
    positions: list[PositionView]
    def to_dict(self) -> dict: ...

def get_cash_balance(client) -> float                     # USDC, already /1e6
def get_positions(client, *, include_resolved: bool = False) -> list[PositionView]
def get_portfolio_summary(client) -> PortfolioSummary
def find_position(client, *, token_id: str | None = None,
                  condition_id: str | None = None, outcome: str | None = None) -> PositionView | None
def get_redeemable(client) -> list[PositionView]
def redeem_all(client, notifier=None) -> list[dict]       # {condition_id, title, ok, error}
```
Note: a resolved-and-lost position has `cur_price == 0` and `redeemable == True`;
redeeming it returns nothing. Say so rather than implying free money.

### `polymarket_bot/trading.py` (rewrite; keep `MAX_ORDER_USDC` importable)
```python
Side = Literal["BUY", "SELL"]
Outcome = Literal["yes", "no"]

@dataclass
class OrderPlan:
    kind: Literal["market", "limit"]
    side: Side
    market_title: str; condition_id: str; token_id: str; outcome_label: str
    usdc_amount: float | None      # BUY
    shares: float | None           # SELL, or limit size
    limit_price: float | None
    est_price: float | None; est_shares: float | None; est_proceeds: float | None
    min_order_size: float; tick_size: float
    warnings: list[str]; blockers: list[str]   # blockers => refuse to execute
    def is_executable(self) -> bool: return not self.blockers
    def to_dict(self) -> dict: ...
    def to_text(self) -> str: ...

@dataclass
class TradeResult:
    ok: bool; status: str | None; order_id: str | None
    filled_shares: float; filled_usdc: float; avg_price: float | None
    tx_hashes: list[str]; error: str | None; plan: OrderPlan
    def to_dict(self) -> dict: ...

def build_buy_plan(client, settings, *, market, outcome: Outcome,
                   usdc_amount: float, limit_price: float | None = None) -> OrderPlan
def build_sell_plan(client, settings, *, market, outcome: Outcome,
                    shares: float | None = None, fraction: float | None = None,
                    limit_price: float | None = None) -> OrderPlan
    # exactly one of shares/fraction; fraction=1.0 means "sell all"; reads the
    # live position to resolve size and blocks if holding is insufficient.
def execute_plan(client, settings, plan: OrderPlan, *, notifier=None) -> TradeResult
def cancel_all_orders(client) -> dict
def list_open_orders(client) -> list[dict]
```
Blockers (refuse execution) vs warnings (inform only):
- blocker: over `settings.max_order_usdc`; market not accepting orders;
  insufficient shares to sell; insufficient cash (respecting
  `min_cash_reserve_usdc`); would exceed `max_position_usdc` in that market;
  limit price not a multiple of tick size / outside `(0,1)`.
- warning: below market minimum order size; wide spread; thin book;
  resolving very soon.

### `polymarket_bot/rules.py`
```python
RuleKind = Literal["take_profit", "stop_loss", "trailing_stop", "time_exit"]

@dataclass
class ExitRule:
    id: str                      # short uuid4 hex
    condition_id: str; token_id: str; market_title: str; outcome: str
    kind: RuleKind
    target_price: float | None   # absolute price trigger
    target_pct: float | None     # +25 => +25% vs avg entry; -20 => -20%
    trail_pct: float | None      # trailing_stop only
    exit_fraction: float = 1.0   # portion of holding to exit
    high_water_mark: float | None = None   # maintained for trailing_stop
    expires_at: str | None = None          # ISO; time_exit uses this
    active: bool = True
    created_at: str = ...; note: str | None = None
    def to_dict(self)/from_dict(d) ...

@dataclass
class RuleDecision:
    should_exit: bool; reason: str; exit_shares: float
    trigger_price: float | None; rule_id: str
    def to_dict(self) -> dict: ...

class RuleStore:                 # JSON at settings.rules_path, atomic writes
    def __init__(self, settings): ...
    def list(self, *, active_only: bool = False) -> list[ExitRule]
    def add(self, rule: ExitRule) -> ExitRule
    def get(self, rule_id: str) -> ExitRule | None
    def update(self, rule: ExitRule) -> ExitRule
    def remove(self, rule_id: str) -> bool
    def for_token(self, token_id: str) -> list[ExitRule]

def evaluate_rule(rule: ExitRule, position: PositionView, current_price: float) -> RuleDecision
    # pure function, no I/O. Mutating high_water_mark is the caller's job:
    # return the new HWM via RuleDecision? No — evaluate_rule must NOT mutate.
    # For trailing stops the monitor updates rule.high_water_mark then persists.
def make_rule(*, kind, position: PositionView, target_price=None, target_pct=None,
              trail_pct=None, exit_fraction=1.0, expires_at=None, note=None) -> ExitRule
```
Semantics (be exact):
- `take_profit`: fire when `current_price >= target` (target from
  `target_price`, or `avg_price * (1 + target_pct/100)`).
- `stop_loss`: fire when `current_price <= target` (`target_pct` negative,
  e.g. `-30` → `avg_price * 0.70`).
- `trailing_stop`: track max price seen in `high_water_mark`; fire when
  `current_price <= high_water_mark * (1 - trail_pct/100)`.
- `time_exit`: fire when `now >= expires_at`.
- Never fire on a resolved position (`is_resolved`) — redeem instead.

### `polymarket_bot/analytics.py`
```python
@dataclass
class TradeStats:
    total_trades: int; wins: int; losses: int; win_rate: float
    total_pnl: float; avg_win: float; avg_loss: float
    best: dict | None; worst: dict | None
    def to_dict(self) -> dict: ...

@dataclass
class Insight:
    headline: str; detail: str; severity: Literal["info","warn","good"]
    def to_dict(self) -> dict: ...

@dataclass
class Opportunity:
    condition_id: str; token_id: str; market_title: str; slug: str | None
    outcome: str; price: float; spread: float | None
    liquidity: float | None; volume_24h: float | None
    daily_reward: float; days_left: int | None
    score: float; reasons: list[str]
    def to_dict(self) -> dict: ...

def get_trade_stats(client, *, limit: int = 500) -> TradeStats
def get_insights(client, *, settings=None) -> list[Insight]
def find_opportunities(client, *, limit: int = 25, max_price: float = 0.95,
                       min_price: float = 0.02, keyword: str | None = None) -> list[Opportunity]
```
`find_opportunities` scores **tradability and structural edge only** — tight
spread, real liquidity, meaningful reward rate, sane time horizon. It must not
claim to predict outcomes, and `reasons` must state what drove the score.
`get_insights` reads the owner's own history — report it straight, including
losing patterns (e.g. repeated short-horizon "Up or Down" markets).

### `polymarket_bot/monitor.py`
```python
@dataclass
class MonitorReport:
    checked_at: str; positions_checked: int; rules_evaluated: int
    triggered: list[dict]; executed: list[dict]; errors: list[str]
    dry_run: bool; halted_reason: str | None
    def to_dict(self)/to_text(self) ...

class Monitor:
    def __init__(self, client, settings, *, notifier=None, store=None): ...
    def run_once(self) -> MonitorReport
    def run_forever(self, *, max_iterations: int | None = None) -> None
```
`run_once` must: re-read live positions; drop/deactivate rules whose position
is gone (sold elsewhere) and say so; update trailing HWMs and persist; evaluate
rules; when `settings.monitor_dry_run` report only; else execute sells via
`trading.execute_plan`. Respect `daily_loss_limit_usdc` — halt executions and
set `halted_reason`. Never let one bad market abort the whole sweep; collect
errors. `run_forever` sleeps `monitor_interval_seconds` between passes and
survives transient API errors with backoff.

### `polymarket_bot/service.py` — the facade a CLI **or Telegram bot** calls
Every function returns a JSON-safe `dict` and never raises for expected
failures — return `{"ok": False, "error": "..."}`. Open and close the client
per call unless one is passed in.
```python
def status() -> dict                 # cash, portfolio totals, open rules, dry-run flag
def positions(*, include_resolved: bool = False) -> dict
def scan(*, limit: int = 20, keyword: str | None = None) -> dict
def briefing(market_ref: str) -> dict
def opportunities(*, limit: int = 15, keyword: str | None = None) -> dict
def analytics() -> dict
def preview_buy(market_ref: str, outcome: str, usd: float, *, limit_price: float | None = None) -> dict
def buy(market_ref: str, outcome: str, usd: float, *, limit_price: float | None = None, confirm: bool = False) -> dict
def preview_sell(market_ref: str, outcome: str, *, shares: float | None = None, fraction: float | None = None, limit_price: float | None = None) -> dict
def sell(market_ref: str, outcome: str, *, shares=None, fraction=None, limit_price=None, confirm: bool = False) -> dict
def set_rule(market_ref: str, outcome: str, kind: str, *, target_price=None, target_pct=None, trail_pct=None, exit_fraction: float = 1.0, note=None) -> dict
def list_rules(*, active_only: bool = False) -> dict
def remove_rule(rule_id: str) -> dict
def monitor_once() -> dict
def redeem() -> dict
def cancel_orders() -> dict
```
`buy`/`sell` must refuse unless `confirm=True`, returning the preview plus
`{"ok": False, "needs_confirmation": True}`. `market_ref` accepts a
condition id (`0x…`) or a slug.

### `polymarket_bot/scripts/*`
Thin CLIs over `service`, printing via `ConsoleNotifier`/plain text. Keep the
existing script names working: `check_connection`, `wallet_status`,
`market_snapshot`, `place_test_order`. Add: `portfolio`, `rules`, `monitor`,
`analyze`. Real orders still require an explicit `--execute` plus either an
interactive `EXECUTE` prompt or `--yes`.
