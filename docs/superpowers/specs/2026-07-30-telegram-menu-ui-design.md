# Telegram menu UI — design

Date: 2026-07-30
Status: approved by user, pending implementation

## Context

The Telegram bot works but is command-only: the owner must remember
`/scan`, `/market <slug>`, `/buy <ref> <yes|no> <usd>`. The user wants it to
feel like an app — browse hot markets, search by keyword, tap through to a
market, and reach Polymarket itself — plus a language toggle (English ⇄
Hebrew).

Verified against the live API before designing:

* `list_markets(order="volume24hr", ascending=False)` returns a genuine
  hot-first ordering. `volumeNum` and `liquidityNum` do not (they sort by
  fields unrelated to recent activity).
* Market URLs: `polymarket.com/market/<market-slug>` returns 200 and
  redirects to the canonical `/event/<event-slug>/<market-slug>`; building
  the canonical form directly also returns 200. `Market.events[0].slug`
  supplies the event slug.
* `list(...iter_items())` pages through the entire result set and hangs.
  Every new call must bound it (`itertools.islice`), as
  `list_tradable_markets` already does with its `break`.

## Goals

1. A browsable menu: hot markets, keyword search, market detail, portfolio.
2. Every market row carries YES price, 24h volume, spread, days left, and a
   working Polymarket link.
3. Buying reachable from the menu, with the existing confirm flow intact.
4. English/Hebrew toggle covering the menu chrome and market rows.

## Non-goals

- Translating `service.py`'s ~200 report/warning/blocker strings. Those are
  safety-critical text; a mistranslated blocker misleads about money. Deep
  reports (`/analyze`, detailed `/status`, buy-preview warnings) stay
  English this round. Decided explicitly with the user.
- Translating market questions (they come from Polymarket in English).
- Any change to the confirm/execute contract. The menu is a new way to
  *reach* `service.buy`/`service.sell`, not a new way to authorise them.
- Alpha/edge features (arbitrage, forecasting) — separate project.

## Design

### 1. Callback namespace split (must land first)

`_handle_callback` currently treats *any* callback containing `:` as a trade
confirmation: it splits on `:` and looks the token up in `self._pending`,
answering "This button already expired" when absent. The moment a menu
button emits `menu:hot:2`, every menu tap hits that path and fails.

So before any menu work: route on the prefix.

* `confirm:<token>` / `cancel:<token>` → existing trade flow, unchanged.
* `nav:<view>:<arg>` → menu navigation.
* Unknown prefix → the existing "Unrecognized action." reply.

This is a pure refactor of the router with the trade path untouched, and it
gets its own tests before the menu exists.

### 2. Callback data budget

Telegram caps `callback_data` at 64 bytes. A market slug alone is often
~58 characters (`will-adanech-abiebie-be-the-next-prime-minister-of-ethiopia`),
so a slug cannot travel in a button.

Menu buttons therefore carry short opaque tokens (`nav:mkt:a1b2c3d4`), with
a per-chat token → market-ref map held in the session (below). Tokens are
minted when a list is rendered and live as long as that session entry does.

### 3. Session state

`MenuSession` per chat id, in memory:

* `view` — which screen is showing
* `page` — pagination offset for the current list
* `query` — the last search keyword (so paging a search re-runs it)
* `refs` — token → market ref (slug/condition id) for the rendered rows
* `awaiting` — set when the bot has asked for typed input (search keyword,
  buy amount), so the next plain text message is routed to that prompt
  instead of ignored
* `touched_at` — for eviction

Sessions are capped and evicted by age; an unbounded dict on a long-running
process is a leak. This is deliberately *not* persisted — it is view state,
and a restart returning the owner to the home screen is correct behaviour.

Language preference **is** persisted (below) because it is a setting, not
view state.

### 4. Language

`telegram/i18n.py`: `t(key, lang, **params)` over a `dict[key][lang]` table.
Every menu string goes through it; `en` and `he` are both required for a key
to exist, so a missing translation is a visible failure at import rather
than a silent English leak at runtime.

Persistence: `data_dir/telegram_prefs.json`, `{"<chat_id>": {"lang": "en"}}`,
written atomically like the rule store. Default **English** (user's choice).
Toggle lives in the ⚙️ More screen.

RTL note: Hebrew rows must not use the fixed-width ASCII table layout from
`scripts/_common.py` — column alignment scrambles under bidi. Menu rows use
a line-per-fact layout, which reads correctly in both directions.

ASCII note: `notify.py` documents that the Windows console is cp1255 and
raises on unencodable characters. Menu strings (Hebrew + emoji) are for the
Telegram transport only and must never be routed into `ConsoleNotifier`.
`ConsoleNotifier` already degrades rather than raising, but the boundary is
worth keeping clean.

### 5. Screens

```
Persistent reply keyboard (every screen):
  🔥 Hot | 🔍 Search | 💼 Portfolio | ⚙️ More

Hot / Search results (5 per page)
  1. Will there be no change in Fed interest rates?
     YES 62%  ·  spread 1.2c  ·  14d left
     24h volume: $1,752,417
     [📊 Details] [🔗 Polymarket]
  ...
  [◀ Prev]  page 1/4  [Next ▶]

Market detail
  full advisor briefing + book
  [Buy YES] [Buy NO] [🔗 Polymarket] [◀ Back]

Buy
  amount prompt → service.buy(confirm=False) preview
  → existing [Confirm] [Cancel] keyboard, confirm_args replayed verbatim

Portfolio   positions, P&L, active rules
More        status, rules, monitor, analyze, redeem, cancel, 🌐 language
```

### 6. Data layer changes

* `markets.list_tradable_markets(client, limit, *, order=None, ascending=False)`
  — passes ordering through to the SDK. Existing callers keep current
  behaviour by omitting the arguments.
* `markets.market_url(market)` — canonical
  `/event/<event-slug>/<market-slug>`, falling back to `/market/<slug>`
  when a market carries no event. Both verified live.
* `service.scan(..., sort="spread"|"hot")` — `"hot"` orders by 24h volume,
  `"spread"` keeps today's tightest-first behaviour and stays the default so
  no existing caller changes meaning. Every row gains `url`.

Keyword search benefits automatically: scanning a volume-ordered window
means a keyword match is found among markets people are actually trading,
rather than in an arbitrary slice.

## Safety properties preserved

* Single-owner lock re-checked on every callback and every text message.
* Buy from the menu is preview → Confirm, never one tap. `confirm_args` are
  replayed exactly as `service` returned them; `_guard_confirmed` still
  applies.
* Confirmation TTL and single-use semantics unchanged.
* The advisor disclaimer still rides on market views.

## Testing

* Router: menu callbacks do not consume trade tokens; trade callbacks still
  work; unknown prefixes are rejected.
* i18n: every key has both languages; no key resolves to a missing string.
* Session: eviction bounds the dict; tokens resolve to the right market;
  a stale token fails cleanly.
* URL builder: canonical form with an event, fallback without one.
* `scan(sort="hot")` orders by volume; `sort="spread"` unchanged (regression).
* Buy-from-menu still produces `needs_confirmation` and sends nothing until
  Confirm.
* Live smoke test against the real bot at the end.

## Sequencing

1. Callback namespace split (+ tests) — unblocks everything
2. i18n + language persistence (+ tests)
3. `market_url`, volume ordering, `scan(sort=)` (+ tests)
4. `menu.py` — sessions, keyboards, renderers (+ tests)
5. Bot wiring: routing, text capture, reply keyboard (+ tests)
6. Full suite + live smoke test
