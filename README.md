# polymarket_bot

A Polymarket **advisor + trading toolkit** for one account, driven from the
command line today and designed to be driven from Telegram tomorrow.

It does five things:

1. **Reads** your account — cash, positions, P&L, resting orders, redeemables.
2. **Explains** live markets in plain English (implied odds, liquidity, horizon,
   maker rewards) and ranks them by how *tradable* they are.
3. **Analyses your own history** — win rate, where the losses actually came
   from, and which habits are structurally expensive.
4. **Trades**, in two steps: build a fully-checked plan, then execute it. Every
   order is capped, re-verified against live state, and requires explicit
   confirmation.
5. **Watches** open positions against stored exit rules (take-profit,
   stop-loss, trailing stop, time exit).

It never predicts outcomes. Scores and briefings describe market *structure*
(spread, depth, volume, time left); nothing in this repo claims to know which
side wins.

---

## Safety model

Read this before running anything with `--execute` or `confirm=True`.

| Guard | Default | Where |
|---|---|---|
| Preview-first | plans are read-only; a separate call sends the order | `trading.build_buy_plan` / `build_sell_plan` vs `execute_plan` |
| Explicit confirmation | `service.buy/sell` refuse without `confirm=True`; CLIs need `--execute` plus typing `EXECUTE` (or `--yes`) | `service.py`, `scripts/` |
| Per-order cap | **$5** (`POLYMARKET_MAX_ORDER_USDC`) | blocker in `trading.py` |
| Per-market position cap | **$10** (`POLYMARKET_MAX_POSITION_USDC`) | blocker in `trading.py` |
| Cash reserve | **$0** (`POLYMARKET_MIN_CASH_RESERVE_USDC`) | blocker in `trading.py` |
| Daily loss limit | **$10** rolling 24h (`POLYMARKET_DAILY_LOSS_LIMIT_USDC`) | halts monitor executions |
| Monitor dry-run | **on** (`POLYMARKET_MONITOR_DRY_RUN=true`) | `monitor.py` reports, sends nothing |
| Live re-check | `execute_plan` rebuilds the plan from fresh state and refuses if anything changed | `trading.execute_plan` |

### Blockers vs warnings

Every plan sorts its objections into two piles. This distinction is the core of
the safety model.

* **Blockers — execution is refused. No flag overrides them.**
  * order over the per-order cap (buys: amount; sells: estimated proceeds)
  * market not accepting orders
  * insufficient cash after the reserve, or the balance could not be read
  * would push this market's cost basis over the position cap
  * selling shares you do not hold, or a size that rounds to 0
  * limit price off the tick grid, or not strictly inside (0, 1)

* **Warnings — informational; the order can still be sent.**
  * size below the market minimum (often 5 shares — a $1 order usually fails)
  * wide spread (>3c), thin or shallow book for the size
  * market resolves within about a day
  * you already hold cost basis in this market
  * the market has settled — redeem instead of selling
  * a limit price that would cross and fill immediately instead of resting

A plan prints `STATUS: REFUSED` when it holds blockers and
`STATUS: executable - not sent yet` otherwise.

### What is *not* protected

* Stop-losses and trailing stops exist only while `monitor` is running (see
  [Exit rules](#exit-rules)). Laptop asleep = no protection.
* The daily loss budget only counts exits **the monitor executed**. Losses you
  take by hand on the website are invisible to it.
* Positions change outside the bot. Everything re-reads live state before
  acting, but a fill can still land between the read and the order.
* A resolved-but-unredeemed position still counts toward the per-market
  position cap until you redeem it.

---

## Setup

```powershell
cd d:\POLYMARKET\claude-ai-poly
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then edit .env in your own editor
```

Requires Python 3.14 (tested on 3.14.4, Windows). Declared dependencies are
`polymarket-client>=0.2.0` and `python-dotenv`; `requests` and `eth_account`
(used by `wallet_status`) come in transitively with the SDK. No other pip
packages are used anywhere in the codebase.

### The `.env` gotcha that costs an afternoon

`.env` needs two credentials:

| Variable | What it is |
|---|---|
| `POLYMARKET_PRIVATE_KEY` | The key that **signs** orders (your EOA / MetaMask key). API credentials are derived from it on every run — nothing else to configure. |
| `POLYMARKET_WALLET` | Your Polymarket **deposit (proxy) wallet address** — the address that actually holds your USDC. |

> **`POLYMARKET_WALLET` is usually NOT your MetaMask account address.**
> When you log into Polymarket with a browser wallet, Polymarket creates a
> separate proxy/Safe contract wallet that your key controls, and the money
> lives there. Put your MetaMask address in this field and everything still
> authenticates — you just get a $0 balance and no error message. That symptom
> (auth works, balance is zero) means the wrong address, not a broken key.
> Find the right one on polymarket.com under Deposit / Settings.

Every other variable is optional — see `.env.example` for the full list with
defaults (risk limits, monitor settings, data dir, Telegram placeholders).

### Verify

```powershell
python -m polymarket_bot.scripts.check_connection
```

Prints the deposit wallet and its USDC collateral balance. If the balance is
zero but you know the account is funded, run:

```powershell
python -m polymarket_bot.scripts.wallet_status
```

It prints the **signer** address and the **configured deposit wallet** side by
side, plus on-chain balances across Ethereum / Base / Arbitrum / Optimism /
Polygon — which catches both the wrong-wallet mistake and money parked on the
wrong chain. On a correctly configured account the two addresses differ:

```
Signer (EOA)              : 0x4370a21bf3571280f8aB6b5DCe354234C2B5D0e0
Configured deposit wallet : 0x512Dc292bF7677A7B9893cCCfAbE638Cc6C5dC85
```

### SDK note: `py-clob-client` is dead

This project originally used `py-clob-client`, which Polymarket has since
**archived**. It no longer works for placing orders. Everything here is built
on the official unified SDK **`polymarket-client`** — package name
`polymarket-client`, **import name `polymarket`**:

```python
from polymarket import SecureClient, Market, Position
```

`SecureClient` also exposes all public read methods, so there is no separate
public client. Verified SDK call signatures live in `CONTRACTS.md`.

---

## Command reference

Everything runs as `python -m polymarket_bot.scripts.<name>` from an activated
venv at the repo root. Every one-shot script takes `--help` and `--json` (raw
service response instead of a report) and exits `0` on success / `1` on
failure. `telegram_bot` is the exception — it's a long-running server, not a
report, so it takes `--help` and `--max-iterations` (mainly for testing)
instead.

| Script | Purpose | Writes? |
|---|---|---|
| `check_connection` | Credentials + account smoke test | no |
| `wallet_status` | Signer vs deposit wallet, multi-chain balances | no |
| `market_snapshot` | Scan markets, or deep-dive one | no |
| `portfolio` | Holdings, P&L, settled/redeemable rows | no |
| `analyze` | Trading record, insights, tradability ranking | no |
| `rules` | Add / list / remove exit rules (local JSON) | local file only |
| `monitor` | Evaluate rules against live prices | only with `--execute` |
| `place_test_order` | Preview or place one order | only with `--execute` |
| `redeem` | Claim every settled position | yes (cannot lose money) |
| `cancel` | Cancel every resting order | yes (cannot lose money) |
| `telegram_bot` | Chat front end over all of the above (see [Telegram bot](#telegram-bot)) | only after tapping Confirm |

### Diagnostics

```powershell
python -m polymarket_bot.scripts.check_connection
python -m polymarket_bot.scripts.wallet_status
python -m polymarket_bot.scripts.wallet_status --skip-chains   # Polymarket only, much faster
```

`check_connection` on a working setup:

```
CONNECTION OK - credentials authenticate and the account reads back.
  Deposit wallet : 0x512Dc2...C5dC85

ACCOUNT
  Cash        : $21.83 USDC
  Positions   : $0.00 across 0 open
  Total value : $21.83
  P&L         : unrealized -$13.93 | realized +$2.18
  Redeemable  : 9 position(s) worth $0.00 - a resolved loser redeems for $0
  Open orders : 0 resting
  Exit rules  : 0 active
  Monitor     : DRY RUN - evaluates only, sends nothing
  Limits      : max order $5.00 | max per market $10.00 | cash reserve $0.00
```

### Account and markets

```powershell
# Holdings, cost, value, P&L, plus the settled/redeemable section
python -m polymarket_bot.scripts.portfolio
python -m polymarket_bot.scripts.portfolio --hide-settled

# Scan tradable markets, tightest spread first
python -m polymarket_bot.scripts.market_snapshot --limit 20
python -m polymarket_bot.scripts.market_snapshot --search iran

# Deep-dive one market: condition id (0x...), slug, or a pasted polymarket.com URL
python -m polymarket_bot.scripts.market_snapshot --market will-jesus-christ-return-before-gta-vi-665

# Your record + insights + the most tradable markets right now
python -m polymarket_bot.scripts.analyze
python -m polymarket_bot.scripts.analyze --stats-only
python -m polymarket_bot.scripts.analyze --opportunities-only --limit 25 --keyword bitcoin
```

### Orders

Dry-run by default: the plan is priced, every risk check runs, nothing is sent.

```powershell
# Preview a $1 buy of 'No' (nothing placed)
python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side no --usd 1

# A resting bid instead of crossing the spread
python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --usd 1 --limit-price 0.42

# Sell previews
python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --sell --shares 10
python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --sell --all

# A take-profit that RESTS on the exchange and works while the bot is offline
python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side yes --sell --all --limit-price 0.80

# Place it for real: --execute, then type EXECUTE at the prompt
python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side no --usd 1 --execute

# Same, non-interactive (only when this exact trade was already approved)
python -m polymarket_bot.scripts.place_test_order --market <slug-or-0x> --side no --usd 1 --execute --yes
```

`--side` accepts `yes` / `no` **or the market's own label** (`up`, `down`, …).
`--all` and `--shares` are mutually exclusive and require `--sell`.

Redeem and cancel have their own commands. Neither takes `--execute`: claiming
a settled position cannot lose money, and cancelling only removes orders that
never filled.

```powershell
# Claim every settled position
python -m polymarket_bot.scripts.redeem

# Cancel every resting order
python -m polymarket_bot.scripts.cancel
```

`redeem` claims settled positions. **A position that lost is redeemable and
pays $0** — redeeming clears it from the list, it does not recover the loss.
`cancel` also kills any take-profit that was resting on the book — the only
exit that works while the bot is offline — so it warns before it acts. A
partial cancel (an order still resting) exits 1, not 0.

### Exit rules (`rules`)

```powershell
python -m polymarket_bot.scripts.rules list
python -m polymarket_bot.scripts.rules list --active

# Take-profit: 50% above your average entry, or at an absolute price
python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind take_profit --pct 50
python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind take_profit --price 0.80

# Stop-loss 30% below entry (--pct is negative)
python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind stop_loss --pct -30

# Trailing stop: exit 20% below the highest price seen
python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind trailing_stop --trail 20

# Time exit, selling only half the holding
python -m polymarket_bot.scripts.rules add --market <ref> --side yes --kind time_exit --expires 2026-08-01T12:00:00Z --fraction 0.5

python -m polymarket_bot.scripts.rules remove a1b2c3d4
```

Adding a rule reads the live position first and refuses if you do not hold that
outcome — a rule with nothing to sell is not protection. Nothing is sent to the
exchange; this is local bookkeeping.

### Monitor

```powershell
# One dry-run sweep and exit
python -m polymarket_bot.scripts.monitor --once

# Keep sweeping until Ctrl-C, still dry-run
python -m polymarket_bot.scripts.monitor
python -m polymarket_bot.scripts.monitor --interval 30

# Actually send the sells triggered rules ask for (typed confirmation first)
python -m polymarket_bot.scripts.monitor --execute
python -m polymarket_bot.scripts.monitor --once --execute --yes
```

**Dry-run is forced unless `--execute` is passed.** The script pins the dry-run
flag from its own arguments, so a `.env` containing
`POLYMARKET_MONITOR_DRY_RUN=false` cannot make a plain `monitor` run live.
Failed passes back off exponentially; Ctrl-C stops cleanly and reminds you that
the rules are no longer enforced.

### Driving it from Python

The scripts are thin wrappers over `service.py`, which is also what a Telegram
bot will call. Every function returns a JSON-safe dict with a ready-to-print
`text` field:

```powershell
python -c "from polymarket_bot import service; print(service.status()['text'])"
python -c "from polymarket_bot import service; print(service.positions(include_resolved=True)['text'])"
python -c "from polymarket_bot import service; print(service.opportunities(limit=10)['text'])"
python -c "from polymarket_bot import service; r=service.buy('slug','no',1.0); print(r['needs_confirmation'], r['text'])"
```

---

## Exit rules

A rule is a stored *intention*, kept as JSON in `data/exit_rules.json`. It is
not a prediction and not a guarantee — it says "when the observed price (or the
clock) reaches this level, try to sell this much".

| Kind | Fires when | Configured with |
|---|---|---|
| `take_profit` | `price >= target` | `target_price`, or `target_pct` (e.g. `+25` → `avg_price * 1.25`) |
| `stop_loss` | `price <= target` | `target_price`, or negative `target_pct` (e.g. `-30` → `avg_price * 0.70`) |
| `trailing_stop` | `price <= high_water_mark * (1 - trail_pct/100)` | `trail_pct` |
| `time_exit` | `now >= expires_at` (UTC) | `expires_at` |

Details that matter:

* `exit_fraction` (default `1.0`) sets how much of the holding to exit.
* A trailing stop's `high_water_mark` is seeded with `max(entry, current price)`
  and ratcheted up on every sweep. The new high is **persisted before** the rule
  is evaluated, so a crash cannot roll the stop back down.
* `make_rule` rejects incoherent rules up front: a take-profit at or below
  entry, a stop-loss at or above entry, a trailing stop without a trail, a time
  exit in the past.
* Rules never fire on a **resolved** position — a settled market is redeemed,
  not sold. The monitor retires such rules and says so.
* The monitor also retires a rule when the position is gone (sold on the
  website), when the holding is dust, and after the exit fills. One rule fires
  once.
* Prices come from the order-book **midpoint**, falling back to best bid, best
  ask, then the data-api last price. The midpoint is used on purpose: on a
  one-sided book a single lowball bid would otherwise trip every stop.

### Polymarket has no native stop orders

This is the single most important architectural fact about this feature.

| | Lives on the exchange? | Works while the bot is off? |
|---|---|---|
| Take-profit as a **resting SELL limit order** (`sell(..., limit_price=...)`) | Yes | **Yes** |
| `take_profit` **rule** | No | No |
| `stop_loss` rule | No | No |
| `trailing_stop` rule | No | No |
| `time_exit` rule | No | No |

Polymarket's CLOB accepts limit orders, so a take-profit can genuinely rest on
the book and fill while your machine is asleep:

```powershell
# Offline-safe take-profit: a resting SELL limit at 0.80
python -m polymarket_bot.scripts.place_test_order --market <ref> --side yes --sell --all --limit-price 0.80 --execute
```

**There is no stop order.** A stop-loss, trailing stop or time exit exists only
in `rules.py` plus the polling loop in `monitor.py` — if the monitor is not
running, those rules protect nothing at all. And even when it is running, a stop
is an instruction to *try* to sell at the next sweep; the fill happens at
whatever the book offers, which in a gap can be much worse than the trigger
price.

The two forms are not interchangeable: a resting limit order locks the shares on
the exchange until it fills or you cancel it, while a stored rule leaves them
free but only acts when the monitor sees the price.

---

## Architecture

Three layers, one rule each.

```
scripts/           printing, argparse, interactive prompts
      |
service.py         facade: one function per user intent, returns JSON-safe dicts
      |            every response carries a ready-to-print "text" field
      v
core modules       no printing, no input(); return dataclasses, emit via Notifier
```

* **Core never prints.** `portfolio`, `trading`, `rules`, `monitor`, `analytics`,
  `markets`, `advisor` return dataclasses (each with `to_dict()` producing
  JSON-safe primitives) and emit events through a `Notifier`.
* **`service.py` never raises.** Expected failures come back as
  `{"ok": False, "error": "..."}`; `_safe` catches the unexpected ones. It
  opens and closes the client per call unless you pass one in.
* **Scripts are thin.** They parse arguments, call one service function, and
  print `response["text"]`.

That is what makes a Telegram bot a *thin layer* over `service.py`: a chat
handler turns a message into one service call and replies with `text`. No
trading logic, no formatting, no error handling on the transport side.

| Module | Role |
|---|---|
| `config.py` | `Settings` (frozen) + `load_settings()` — credentials, risk limits, paths |
| `notify.py` | `Notifier` protocol; Null / Console / Collecting / Multi sinks |
| `client.py` | `get_client() -> SecureClient` |
| `markets.py` | Read-only market data: markets, books, spreads |
| `advisor.py` | Plain-English briefings + the standing disclaimer |
| `portfolio.py` | Cash, positions, P&L, redemption (`redeem_all` is its only write) |
| `trading.py` | `OrderPlan` / `TradeResult`; **the only order write path** |
| `rules.py` | `ExitRule`, `RuleStore` (atomic JSON), pure `evaluate_rule` |
| `monitor.py` | Polling sweep, trailing ratchet, loss budget, execution |
| `analytics.py` | Trade stats, insights, tradability scoring |
| `service.py` | The facade everything above is called through |
| `scripts/_common.py` | CLI plumbing only: argparse helpers, ASCII tables, `EXECUTE` prompt, exit codes |

Local state (git-ignored, `POLYMARKET_DATA_DIR`, default `data/`):

* `exit_rules.json` — stored rules
* `monitor_state.json` — rolling ledger of monitor-executed exits, for the
  daily loss budget

### Telegram bot

```
python -m polymarket_bot.scripts.telegram_bot
```

A thin command router over `service.py` — it cannot do anything the CLI
couldn't already do. Built on `requests` (already a dependency); no new
package was added.

**First run:**
1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token
   into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Run the script, then message the bot anything (`/start` is fine).
3. It replies with your chat id. Put that in `TELEGRAM_CHAT_ID` in `.env`.
4. Restart. It now answers **only** that chat — every other chat is silently
   ignored, with no reply at all, so a stranger who finds the bot's username
   gets nothing back, not even an error.

**Commands:** `/status` `/positions [all]` `/scan [kw]` `/market <ref>`
`/opportunities [kw]` `/analyze` `/buy <ref> <yes|no> <usd> [limit]`
`/sell <ref> <yes|no> <all|shares> [limit]`
`/rule <ref> <yes|no> <kind> <pct|price|trail> <value>` `/rules`
`/removerule <id>` `/monitor` `/redeem` `/cancel` `/help`.

**Confirm flow:** `/buy` and `/sell` call `service.buy`/`service.sell` with
`confirm=False` — which prices the order and returns it *unsent* — and attach
an inline **Confirm / Cancel** keyboard. Tapping Confirm replays the exact
`confirm_args` the preview returned; it is never re-derived from the typed
command, so a fill landing between preview and tap can't silently trade more
than what was shown (`service._guard_confirmed` still checks this on top). A
confirmation is single-use and expires after 2 minutes — a replayed or stale
tap edits the message to say so and sends nothing.

**Latency:** the bot holds one `SecureClient` for the process lifetime and
passes it into every `service` call (`client=self.client`), so a command
doesn't pay for a fresh auth handshake each time — that was the dominant cost.
Updates are long-polled, which delivers close to instantly without needing a
public HTTPS endpoint or TLS certificate.

**Push alerts:** with both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set,
`monitor` in continuous mode fans its events out to the console *and* that
chat (`telegram/notifier.py`, via `MultiNotifier`):

```powershell
python -m polymarket_bot.scripts.monitor            # dry run, still pushes
python -m polymarket_bot.scripts.monitor --execute   # live exits, pushes fills
```

The header line says `Alerts : console + Telegram` when it's on. Three details
worth knowing:

* **Only `trade` / `alert` / `error` are pushed.** A sweep runs every interval;
  forwarding its routine chatter would train you to ignore the one message that
  matters. The console keeps the full stream.
* **It talks to Telegram directly.** The `telegram_bot` process does not need
  to be running — this is an outbound HTTP call, not a message through the bot.
* **A failed send is swallowed, never raised.** The monitor emits through the
  notifier *while executing a sell*; a Telegram outage must not abandon an exit
  half-done. The trade still happens; you just don't get the message.

`--json` mode never pushes — that output is for scheduled or piped runs.

---

### Running without a computer left on

Stop-loss and trailing-stop rules are enforced only while `monitor` is an
actually-running process — see
[Polymarket has no native stop orders](#polymarket-has-no-native-stop-orders).
A laptop asleep is a monitor that isn't running, and push alerts do not change
that: they tell you what a *running* monitor did, they do not enforce anything
on their own. Options, roughly in order of how much they cost:

| Option | Cost | True 24/7? | Notes |
|---|---|---|---|
| Windows Task Scheduler | free | No | PC must stay awake; sleep = no protection |
| Raspberry Pi at home | ~$50 once | Yes | Low power, the private key stays on hardware you hold |
| Small VPS (Hetzner, DigitalOcean, …) | ~$4–6/mo | Yes | Also solves Telegram hosting — one box runs both |
| Oracle Cloud free tier | free | Yes | Same as a VPS, if a free instance is available |

The real tradeoff with a VPS or always-on Pi is that `POLYMARKET_PRIVATE_KEY`
then lives on a machine you don't have your hands on. That's a reasonable
trade for a small, dedicated trading wallet — it is **not** a reasonable trade
for a wallet holding anything you'd mind losing. Use a wallet sized for that
before putting its key on a server.

## Limitations (honest list)

* **No forecasting.** Opportunity scores rank tradability — spread, resting
  liquidity, 24h volume, maker rewards, time to resolution. A market can score
  90 and still be a bad bet. Nothing here knows which outcome wins.
* **Stops need the monitor.** See above. There is no exchange-side stop.
* **The loss budget is partial.** It counts only exits the monitor executed.
* **Monitor P&L excludes fees** and treats the position's average entry as the
  cost basis, so it is a budgeting estimate, not accounting truth.
* **Minimum order size** is often 5 shares, so a $1 order only clears it on
  low-priced outcomes. Plans warn, the exchange rejects.
* **The rule store is local.** It is not synced to Polymarket and knows nothing
  about orders you place on the website.
* **History is capped** at the last 500 fills/positions by default
  (`get_trade_stats(limit=...)`).
* **Single account, single machine.** No concurrency control beyond atomic file
  writes; two monitors against one data dir will fight.

## Roadmap

* Optionally mirror a `take_profit` rule as a real resting limit order so it
  survives the bot being offline.
* Run the monitor (and the Telegram bot) as a service on whatever host was
  chosen in [Running without a computer left on](#running-without-a-computer-left-on).
* Maker-side strategy for CLOB liquidity rewards (currently 100% of fills on
  this account are taker fills, which pay the spread and earn no rewards).
* A strategy layer that seeks actual edge — arbitrage between correlated
  markets, mispricing signals, forecasting. Nothing here does this today: the
  advisor explains structure and ranks *tradability*, and deliberately makes no
  prediction about which outcome wins (`advisor.py`).
* Deeper coverage of the remaining `trading.py` / `monitor.py` / `service.py`
  surface. The money paths are pinned (`tests/test_trading.py`,
  `tests/test_monitor.py`, `tests/test_service.py`); the reporting and
  formatting paths are still only covered indirectly.

> Not financial advice. Prediction markets are speculative and you can lose your
> entire stake. Only risk money you can afford to lose.
