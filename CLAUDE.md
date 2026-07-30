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
- ✅ 184/184 tests passing
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
```

**What's left to do:**
- Choose where monitor runs 24/7 (VPS ~$5/mo, Raspberry Pi, or always-on PC).
  Until then, stop-losses are only enforced while the monitor is running here.
- Strategy layer — arbitrage between correlated markets, mispricing signals,
  forecasting. **Not yet designed**; needs its own brainstorm + spec. Note the
  advisor is deliberately non-predictive today (see `advisor.py`'s docstring),
  so this is a genuine addition, not a tweak.
- Optionally mirror a take-profit as a resting limit order so it survives the
  bot being offline.

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
