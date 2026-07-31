# Claude Code Guidelines for Polymarket Bot

This file documents how to work on this project effectively. Decisions here reflect lessons learned from building a production trading bot where bugs can cost real money.

## Current Status (Session 2026-07-30)

**What's built and working:**
- ✅ Full trading system: buy YES/NO, sell (whole or partial), limit orders
- ✅ Exit rules: take-profit, stop-loss, trailing-stop, time-exit
- ✅ Monitor that enforces rules (dry-run by default, `--execute` for live)
- ✅ Read-only analytics: portfolio, trade history, market scanning, opportunities
- ✅ Telegram bot: thin router over service.py, inline Confirm/Cancel buttons, single-owner lock
- ✅ **Push alerts**: monitor fans trade/alert/error events out to Telegram
- ✅ **Standalone `redeem` / `cancel` CLI commands**
- ✅ **Telegram menu UI**: hot markets, keyword search, market detail, buy flow,
  watchlist, English/Hebrew toggle
- ✅ **Arbitrage detection** (`arbitrage.py`): taker scan (both asks) and maker
  pairs (both bids), priced from real order books
- ✅ 403/403 tests passing
- ✅ Safety guardrails: per-order caps, position caps, daily loss limit, slippage guards
- ✅ Fee disclosure: warns users of 5% taker fees on high-fee markets
- ✅ Cost-basis lag protection: detects fresh fills before entry price is populated

**How to run:**
```bash
# Start the Telegram bot (listens for messages, confirms with inline buttons)
python -m polymarket_bot.scripts.telegram_bot

# Watch positions and enforce exit rules; pushes to Telegram when configured
python -m polymarket_bot.scripts.monitor              # dry run, continuous
python -m polymarket_bot.scripts.monitor --execute    # live exits

# Or run CLI commands for testing:
python -m polymarket_bot.scripts.check_connection      # verify account
python -m polymarket_bot.scripts.portfolio             # show holdings
python -m polymarket_bot.scripts.monitor --once        # dry-run rule sweep
python -m polymarket_bot.scripts.place_test_order ...  # preview/execute orders
python -m polymarket_bot.scripts.redeem                # claim settled positions
python -m polymarket_bot.scripts.cancel                # cancel resting orders
python -m polymarket_bot.scripts.arbitrage             # YES+NO under $1 (taker)
python -m polymarket_bot.scripts.arbitrage --maker     # the bid side (common)
```

## Arbitrage: what is real and what is not

Measured against live books on 2026-07-31, and the numbers drove the design:

- **Taker arb (paying both asks) essentially does not exist.** Across 23 liquid
  markets every pair priced 100.1¢–102¢; none were below par. The ~1¢ over is
  the spread you cross twice. Do not expect this scan to find anything — when
  it does, suspect a stale book before celebrating.
- **Maker pairs (resting a bid on both sides) exist nearly everywhere.** All 23
  had a positive gap, median 1.00¢. This is not the venue leaving money out:
  the gap is a market maker's compensation for fill risk and adverse selection.
  You collect it only by bearing those.
- **Fees decide it.** Taker fees reach 5%, larger than any edge either scan
  finds. `net_edge` subtracts an estimate and the report says when fees eat it.
- **Never price from gamma quotes.** `market.outcomes.*.price` is mid/last. An
  edge computed from a midpoint is not there at the touch. Always walk the book.
- **Liquidity reward rates are the market's shared daily pool**, split across
  every provider — not an individual payout. Wording it otherwise reads as
  "$1000/day" on an account holding $21.
- **Neither finder can trade, on purpose.** Both legs must fill, there is no
  atomic two-leg order on Polymarket, and a half-filled pair is a naked
  directional position. No execution path exists in `arbitrage.py`.

**What's left to do:**
- Choose where monitor runs 24/7 (VPS ~$5/mo, Raspberry Pi, or always-on PC).
  Until then, stop-losses are only enforced while the monitor is running here.
- Optionally mirror a take-profit as a resting limit order so it survives the
  bot being offline.
- A maker-side *strategy* (as opposed to the current detector): actually
  posting both legs, tracking partial fills, and unwinding a leg that fills
  alone. That is a real trading system with its own risk model, not a report.
- Forecasting / correlated-market mispricing. Still not designed, and note the
  advisor stays deliberately non-predictive (`advisor.py`).

## Running the bot (read before restarting it)

**Only ever run ONE bot process.** Several pollers on one token get handed
updates at random, so the owner's taps are answered by whichever process asked
first — including one running older code. This produced "unknown command
/menu" for a command that existed, and "this confirmation is no longer valid"
for a freshly rendered button. `telegram/singleton.py` now refuses to start a
second one; the lock lives at `data/telegram_bot.lock`.

**Killing the shell does not kill the bot.** `venv/Scripts/python.exe` is a
launcher that spawns the real interpreter as a *child*, so stopping the shell
pipeline leaves that child polling. Four accumulated this way. To stop it:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*telegram_bot*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Then confirm none remain before starting a new one.

## Security & Secrets
- **Never ask the user to paste secrets into chat.** Not POLYMARKET_PRIVATE_KEY, wallet addresses, Telegram tokens, or anything from `.env`.
- **Create `.env` from `.env.example`, don't expose it.** User fills their own values locally.
- **Never print `.env`, credentials, or private keys** even for debugging. Use masked output (e.g., `0x512Dc2...C5dC85`).
- `.env` and `data/` are git-ignored. Keep them that way.

## Testing & Verification
- **Every money-touching feature needs three levels of proof:**
  1. **Offline probe**: test double or fake SDK, proves old code fails
  2. **Live-fire test**: real account, real order (small amount), verify execution
  3. **Regression test**: added to test suite to prevent backsliding
- **Run the full test suite after changes**: `python -m pytest -q`. Must pass before declaring done.
- **Test the CLI path first**, then wire to Telegram if applicable. CLI is the reference implementation.
- **Stub the seam the code actually reads.** A test that patches an attribute
  which doesn't exist still passes — silently, forever. `test_empty_positions_reads_never_retire_a_rule`
  stubbed `Monitor._live_positions`, which was never a real method; the sweep
  died on its first read and the assertion held for the wrong reason. When a
  test guards a money bug, prove it fails against the broken behaviour before
  trusting it. The real seams: `portfolio.get_positions` (positions),
  `trading.get_market_by_condition_id` (the re-verify read in `execute_plan`).

## Safety Model
- **Blockers** (prefixed `X BLOCKED`) are refused, no override. Never relax them for speed.
- **Warnings** are honest, not hidden. If something *might* fail, warn the user before they confirm.
- **Fail closed, not open**: when in doubt, refuse the order.
- **All exit rules must be proven to fire**: stop-loss, take-profit, trailing-stop. Live test before claiming done.
- **Monitor runs as a separate process** — stop-losses are inert while it's offline. Document this.

## Code Structure
- **Core logic lives in `service.py` functions**, not in scripts or Telegram bot.
- **Telegram bot is a thin router**: it calls `service.py` functions with `confirm=False` for preview, then replays confirmed args with `confirm=True`.
- **Every service function returns a JSON dict** with `ok` (bool), `text` (for chat), and structured fields.
- **No new dependencies for Telegram**: built on `requests` (already transitive).

## Workflow
1. **Understand the bug/feature deeply** before coding. Read CONTRACTS.md (the spec).
2. **Write the feature/fix with tests** in mind. Tests guide the design.
3. **Run offline probes** to prove the fix works. Don't skip this — it catches regressions early.
4. **Live-fire test** on a real account. Start small ($1 orders).
5. **Add regression test** to `tests/` so it never breaks silently.
6. **Update README.md, CONTRACTS.md** if the contract changed.
7. **Commit with a clear message** explaining *why*, not just *what*.

## Key Gotchas (Hard-Won Lessons)
- **py-clob-client is dead.** The SDK archived it; Polymarket's backend rejects its old EIP-712 domain version. Use `polymarket-client` (official).
- **POLYMARKET_WALLET is NOT the MetaMask address.** It's the proxy/Safe wallet that holds the funds. Wrong one = $0 balance, endless debugging.
- **Market fees can be 5%.** Some markets charge takers heavily. Always preview and warn the user before they confirm.
- **Sub-1-share positions are invisible by default.** `list_positions` hides them unless `size_threshold=0`.
- **Fresh fills have a brief lag.** The API returns `avg_price=0` for a few seconds after a buy/sell. Don't compute P&L against zero.
- **Stop-losses need the monitor running.** Polymarket has no native stops. A stopped monitor = unprotected positions.
- **Dust top-of-book kills stop-losses.** A 1-share quote at 0.90 over real liquidity at 0.50 creates an unfillable floor. Anchor slippage to realistic fill price, not just the touch.
- **Empty /positions response is a *failed* read, not proof of a sale.** Don't retire rules on API transients.
- **Rule confirms must be one-shot, not re-derived.** The preview price is the approved price. Re-previewing on tap creates race-condition trades.

## Before You Start
- Copy `.env.example` to `.env` locally (git-ignored).
- Only you fill in the secrets — never ask the user to paste them.
- Run `python -m pytest -q` to verify the environment.
- Read CONTRACTS.md if the feature touches trading, rules, or monitor.

## When You're Done
- Full test suite passes.
- README.md and CONTRACTS.md are up to date.
- User has confirmed it works (live or via our live test).
- Commit message explains *why* the change.

## Communication Style
- Be clear and concise. User's time is valuable.
- Explain tradeoffs, don't hide them.
- If something is risky or untested, say so.
- Ask for confirmation on real trades, even if the bot would allow it.
- Always verify understanding with the user before big steps.
