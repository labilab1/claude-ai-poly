# Hardening sprint — design

Date: 2026-07-30
Status: approved, pending implementation

## Context

The bot (trading, exit rules, monitor, analytics, Telegram front end) is
functionally complete and passes 124/124 tests, but almost the entire
codebase existed only in the working tree, uncommitted. That risk is now
fixed (commit `35bd10b`, local only, not pushed).

Before any work on real alpha-seeking features (arbitrage between
correlated markets, forecasting signals — deliberately out of scope here,
see "Not in this spec"), the user wants the existing system hardened. This
spec covers that hardening slice only.

## Goals

1. Stop-loss/take-profit protection reaches the user's phone, not just a
   terminal that happens to be open.
2. `redeem`/`cancel` are reachable the same way every other capability is
   (a standalone CLI command), not only via Telegram.
3. The three largest, most money-critical modules
   (`trading.py`, `monitor.py`, `service.py` — ~4,100 lines combined) get
   characterization tests on their money-critical paths. Full line coverage
   is explicitly not the goal; see Non-goals.

## Non-goals

- Full test coverage of `trading.py`/`monitor.py`/`service.py`. These are
  large modules; only the paths that touch order placement, confirm-gating,
  and rule-triggered sells are in scope today.
- 24/7 hosting (VPS/Raspberry Pi). Deferred — user has no server yet;
  deployment artifacts were explicitly declined for this session too.
- Any arbitrage, mispricing-detection, or forecasting feature. That is a
  separate, larger initiative the user wants to brainstorm as its own
  spec once this hardening lands.
- Pushing the local commit to `origin`. Stays local until the user asks.

## Design

### 1. Push alerts: monitor → Telegram

**New file:** `polymarket_bot/telegram/notifier.py`

```python
class TelegramNotifier:
    """Implements the Notifier protocol; sends to one chat via the Bot API."""
    def __init__(self, api: TelegramAPI, chat_id: int | str, *, min_level: Level = "trade"): ...
    def send(self, message: str, *, level: Level = "info") -> None: ...
```

- Filters by `min_level` before sending — default `"trade"`, meaning
  `trade`/`alert`/`error` reach Telegram; `debug`/`info` do not. Rationale:
  a sweep happens every `--interval` seconds; only state changes deserve a
  phone notification.
- Uses the existing `chunk_message()` helper for anything over Telegram's
  4096-char limit.
- **Never raises.** Any exception from `TelegramAPI.send_message` (network
  error, bad token, rate limit) is caught and swallowed — optionally
  logged to stderr — because a Telegram outage must not take down the
  monitor's own sell path. Same principle `MultiNotifier` already applies
  to its fan-out.

**Wiring:** `polymarket_bot/scripts/monitor.py`, inside `_forever()`. Today
it always builds `ConsoleNotifier(min_level="debug")`. Change: if
`settings.telegram_bot_token` and `settings.telegram_chat_id` are both set,
build `MultiNotifier(ConsoleNotifier(min_level="debug"), TelegramNotifier(TelegramAPI(token), chat_id))`
instead. `--json` mode is unaffected (still `_JsonNotifier` only — a
scheduled/scripted run isn't the audience for a phone alert).

This keeps `monitor` and the Telegram bot as independent processes, exactly
as documented in README's "Running without a computer left on" — the
monitor talks to Telegram's HTTP API directly and has no dependency on the
bot process being alive.

**Tests:** `tests/test_telegram_notifier.py` — fake `TelegramAPI`
(or a stub session) verifying: (a) level filtering drops `info`/`debug` by
default, (b) a long message is chunked into multiple `send_message` calls,
(c) an exception from the API is swallowed, not propagated, (d) monitor
wiring picks `MultiNotifier` when both env vars are set and plain
`ConsoleNotifier` when they aren't.

### 2. Standalone `redeem`/`cancel` CLI

`service.redeem()` and `service.cancel_orders()` already exist, are fully
implemented, and take no `confirm` parameter — neither can lose money
(redeem only claims; cancel only removes resting orders, no fill risk).
So this is pure exposure, no new business logic.

**New files**, following the existing script pattern in
`polymarket_bot/scripts/_common.py` (`build_parser`, `dispatch`,
`dump_json`, `with_disclaimer`) — same shape as `wallet_status.py`:

- `polymarket_bot/scripts/redeem.py`
  ```
  python -m polymarket_bot.scripts.redeem [--json]
  ```
- `polymarket_bot/scripts/cancel.py`
  ```
  python -m polymarket_bot.scripts.cancel [--json]
  ```
  Prints the existing warning from `service.cancel_orders()`'s response
  text (cancelling removes any take-profit resting as a limit order) before
  the result — the script surfaces what the service already returns, it
  doesn't add new warning logic.

**Tests:** `tests/test_scripts_redeem_cancel.py` — CLI-level tests using a
fake client/service double, following the existing pattern for the other
`scripts/` tests (there isn't one yet for e.g. `portfolio.py`'s script, so
this establishes it): exit code 0 on success, JSON output shape with
`--json`, text output includes the disclaimer.

### 3. Characterization tests for money-critical paths

Scope, explicitly bounded to avoid an open-ended test-writing task:

- `tests/test_trading.py` — `execute_plan`'s per-order cap enforcement
  (buys only, per [[project-polymarket-advisor]]'s documented gotcha),
  the slippage floor on sells (`min_price` anchored to
  `min(touch, estimated_fill)`), and refuse-if-unpriceable fail-closed
  behavior. These three are the exact fixes called out as
  safety-regression-prone in `test_safety_regressions.py`'s sibling module,
  `trading.py` itself, but that file currently has no direct test.
- `tests/test_monitor.py` — a triggered rule producing exactly one sell
  call with the previewed price (not re-derived), and a halted monitor
  (daily loss limit spent) reporting but not selling.
- `tests/test_service.py` — `buy`/`sell` refuse without `confirm=True`
  and return `needs_confirmation`; `_guard_confirmed` refuses when live
  state has drifted beyond what was confirmed.

All three use fakes/doubles (matching the existing `test_rules.py` and
`test_safety_regressions.py` style) — no live API calls, no real orders.

## Testing & validation plan

1. Each new module ships with its test file in the same change (TDD for
   the two new-code pieces: write the failing test, then the
   implementation).
2. `python -m pytest -q` must pass (currently 124; expect to land in the
   150s-160s range after this work) before any piece is considered done.
3. Manual smoke test of `redeem`/`cancel` CLI against the real (small,
   $21.83) account in dry/no-op conditions (e.g., `cancel` with zero
   resting orders, `redeem` with zero settled positions) — proves the
   wiring without needing money at risk.
4. Push-alert wiring is smoke-tested by running `monitor --once` is not
   sufficient (that path doesn't touch the new notifier); instead, a short
   `monitor` continuous run (`--max-passes 1`) with Telegram creds set,
   confirming a message arrives in the real chat.

## Sequencing

1. `TelegramNotifier` + wiring + tests
2. `redeem.py` / `cancel.py` + tests
3. Characterization tests for `trading.py`, `monitor.py`, `service.py`

Each step ends with the full suite green and is a natural commit boundary.

## Out of scope, deferred by explicit user decision

- 24/7 hosting.
- Arbitrage / mispricing / forecasting features — to be brainstormed as
  its own spec once this hardening is complete. The user's capital
  ($21.83) is not treated as a hard constraint on that future design; more
  can be added once the mechanism proves itself.
