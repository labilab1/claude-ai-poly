"""Screens, keyboards and view state for the Telegram menu.

This module is presentation only. It renders what `service.py` returns and
builds the keyboards that navigate between screens; it never decides anything
about money, and it holds no client. That keeps the bot a thin router even now
that it has a UI: a screen is `service call -> render -> keyboard`.

Three constraints shape most of what is here.

**64 bytes of callback_data.** Telegram's hard cap. A market slug alone is
routinely ~58 characters, so a slug cannot travel inside a button. Buttons
carry short opaque tokens instead and the session resolves them back to a
market ref. `PAYLOAD_LIMIT` below is checked by the tests, not assumed.

**Sessions are view state, not settings.** They live in memory, are capped,
and are evicted by age. Losing them on restart is correct - the owner lands
back on the home screen. Language preference is the opposite (a real setting)
and lives in `i18n`'s preferences file instead.

**Right-to-left.** Hebrew rows use one fact per line rather than the aligned
fixed-width tables `scripts/_common.py` builds, because column alignment
scrambles under bidi. The row layout below reads correctly in both directions.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from polymarket_bot.telegram.i18n import t

#: Telegram's cap on callback_data, in bytes.
PAYLOAD_LIMIT = 64

#: Markets shown per page. Five keeps a page inside one message with room for
#: the header, the note and the pagination row.
PAGE_SIZE = 5

#: How long an idle session survives. Long enough that reading a list and then
#: tapping into a market is never interrupted; short enough that a bot running
#: for weeks does not accumulate dead state.
SESSION_TTL_SECONDS = 60 * 60

#: Hard cap on retained sessions. Single-owner today, but an unbounded dict on
#: a long-running process is a leak whatever the access rules are.
MAX_SESSIONS = 32

#: Market tokens kept per session. Enough to page through several screens and
#: still have earlier buttons work; bounded so browsing does not grow it
#: forever.
MAX_REFS = 60

# View names, also used as the `nav:` action segment.
VIEW_HOME = "home"
VIEW_HOT = "hot"
VIEW_SEARCH = "search"
VIEW_MARKET = "mkt"
VIEW_PORTFOLIO = "pf"
VIEW_MORE = "more"
VIEW_LANG = "lang"
VIEW_BUY = "buy"
VIEW_ARB = "arb"
VIEW_WATCH = "watch"
VIEW_WATCHLIST = "wl"

# What the bot is waiting for the next plain text message to be.
AWAIT_SEARCH = "search"
AWAIT_BUY_AMOUNT = "buy_amount"


@dataclass
class MenuSession:
    """One chat's view state. Never persisted - see the module docstring."""

    lang: str = "en"
    view: str = VIEW_HOME
    page: int = 0
    query: str | None = None
    sort: str = "hot"
    #: token -> market ref (slug or condition id) for the rows currently shown.
    refs: dict[str, str] = field(default_factory=dict)
    #: What a plain text message means right now, if anything.
    awaiting: str | None = None
    #: Market ref and outcome a pending buy prompt refers to.
    pending_market: str | None = None
    pending_outcome: str | None = None
    #: The message this chat's UI lives in. Navigation EDITS this message
    #: rather than sending a new one, so the whole menu is a single message
    #: that changes - the chat never fills up with screens, and tapping a
    #: button posts nothing visible at all.
    canvas_id: int | None = None
    #: Last text/keyboard rendered into the canvas. Telegram rejects an edit
    #: that would not change anything ("message is not modified"), so a repeat
    #: tap is answered locally instead of by an API error.
    last_render: tuple[str, str] | None = None
    #: Where Back should go from the market screen.
    back_view: str = VIEW_HOME
    touched_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.touched_at = time.monotonic()

    def remember(self, market_ref: str) -> str:
        """A short token for a market ref, stable for the life of the session.

        Stable, not fresh-per-render, for two reasons. Re-rendering a screen
        must produce an identical keyboard, or the "did anything change?" check
        in `_render` never matches and every repeat tap pushes a pointless edit
        to Telegram. And a token from a screen further up the chat keeps
        working instead of reporting itself expired.
        """
        for token, ref in self.refs.items():
            if ref == market_ref:
                return token
        token = uuid.uuid4().hex[:8]
        self.refs[token] = market_ref
        # Bounded: a long session browsing many pages would otherwise grow this
        # forever. Oldest inserted goes first.
        while len(self.refs) > MAX_REFS:
            self.refs.pop(next(iter(self.refs)))
        return token

    def resolve(self, token: str) -> str | None:
        return self.refs.get(token)

    def clear_prompt(self) -> None:
        self.awaiting = None
        self.pending_market = None
        self.pending_outcome = None


class SessionStore:
    """Per-chat sessions with age and size bounds."""

    def __init__(self, *, ttl_seconds: float = SESSION_TTL_SECONDS, max_sessions: int = MAX_SESSIONS):
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._sessions: dict[int, MenuSession] = {}

    def get(self, chat_id: int, *, lang: str = "en") -> MenuSession:
        self._expire()
        session = self._sessions.get(chat_id)
        if session is None:
            session = MenuSession(lang=lang)
            self._sessions[chat_id] = session
        session.touch()
        # Size is enforced AFTER the insert, or the store settles one over the
        # cap: evicting first leaves room for exactly `_max`, and then adds one.
        # The session just touched is the newest, so it is never the one dropped.
        self._enforce_size()
        return session

    def drop(self, chat_id: int) -> None:
        self._sessions.pop(chat_id, None)

    def _expire(self) -> None:
        now = time.monotonic()
        for chat_id, session in list(self._sessions.items()):
            if now - session.touched_at > self._ttl:
                del self._sessions[chat_id]

    def _enforce_size(self) -> None:
        excess = len(self._sessions) - self._max
        if excess <= 0:
            return
        oldest = sorted(self._sessions.items(), key=lambda kv: kv[1].touched_at)[:excess]
        for chat_id, _ in oldest:
            del self._sessions[chat_id]

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._sessions)


# --------------------------------------------------------------------------
# callback payloads
# --------------------------------------------------------------------------


def nav(view: str, arg: str = "") -> str:
    """Build a `nav:` callback payload, refusing to exceed Telegram's cap.

    Raising here is deliberate: a payload silently truncated by Telegram
    produces a button that navigates somewhere unintended, which is far worse
    to debug than a failure at build time.
    """
    payload = f"nav:{view}:{arg}" if arg else f"nav:{view}:"
    if len(payload.encode("utf-8")) > PAYLOAD_LIMIT:
        raise ValueError(
            f"callback payload {payload!r} exceeds Telegram's {PAYLOAD_LIMIT}-byte limit"
        )
    return payload


def parse_nav(token: str) -> tuple[str, str]:
    """Split the part after `nav:` into (view, arg)."""
    view, _, arg = token.partition(":")
    return view, arg


# --------------------------------------------------------------------------
# keyboards
# --------------------------------------------------------------------------


def reply_labels(lang: str) -> dict[str, str]:
    """Deprecated: the reply keyboard is gone.

    Kept as an empty mapping so any straggling caller degrades to "this text is
    not a menu button" instead of raising. The menu is inline-only now - see
    `home_keyboard` for why.
    """
    return {}


def home_keyboard(lang: str) -> dict:
    """The dashboard. Every destination is one tap from here.

    Inline, not a reply keyboard: an inline tap fires a callback_query, which
    posts NOTHING into the chat. A reply keyboard sends the button's label as
    a message from the user, which fills the conversation with your own words
    and does not read as an application.
    """
    return {
        "inline_keyboard": [
            [
                {"text": t("menu.hot", lang), "callback_data": nav(VIEW_HOT, "0")},
                {"text": t("menu.search", lang), "callback_data": nav(VIEW_SEARCH, "")},
            ],
            [
                {"text": t("menu.portfolio", lang), "callback_data": nav(VIEW_PORTFOLIO, "")},
                {"text": t("btn.watchlist", lang), "callback_data": nav(VIEW_WATCHLIST, "")},
            ],
            [
                {"text": t("btn.arb", lang), "callback_data": nav(VIEW_ARB, "")},
                {"text": t("btn.analyze", lang), "callback_data": nav(VIEW_MORE, "analyze")},
            ],
            [
                {"text": t("menu.more", lang), "callback_data": nav(VIEW_MORE, "")},
                {"text": t("btn.refresh", lang), "callback_data": nav(VIEW_HOME, "")},
            ],
        ]
    }


def nav_row(lang: str, *, back: str | None = None, extra: list[dict] | None = None) -> list[dict]:
    """The row every screen ends with, so no screen is ever a dead end."""
    row: list[dict] = []
    if back is not None:
        row.append({"text": t("btn.back", lang), "callback_data": nav(back, "0")})
    row.extend(extra or [])
    row.append({"text": t("btn.home", lang), "callback_data": nav(VIEW_HOME, "")})
    return row


def text_screen_keyboard(lang: str, *, back: str | None = None, refresh: str | None = None) -> dict:
    """For screens that are just a report: back, optional refresh, home."""
    extra = []
    if refresh is not None:
        extra.append({"text": t("btn.refresh", lang), "callback_data": nav(refresh, "")})
    return {"inline_keyboard": [nav_row(lang, back=back, extra=extra)]}


#: Longest market-button label. Telegram will render more, but a button wider
#: than the screen wraps and the row stops being scannable.
BUTTON_LABEL_WIDTH = 34


def button_label(number: int, question: str) -> str:
    """`3 · Will the Fed cut rates…` - a button that names its own row.

    Five buttons all reading "Details" is the reported defect: the column gave
    no way to tell which market a button opened. Leading with the row number
    ties the button to the numbered text above it, and the truncated question
    makes it readable on its own.
    """
    title = " ".join((question or "").split())  # collapse newlines/runs
    prefix = f"{number} · "
    room = BUTTON_LABEL_WIDTH - len(prefix)
    if len(title) > room:
        title = title[: max(room - 1, 1)].rstrip() + "…"
    return f"{prefix}{title}" if title else str(number)


def market_rows_keyboard(
    entries: list[tuple[str, str | None, int, str]],
    *,
    lang: str,
    page: int,
    pages: int,
    view: str,
) -> dict:
    """Inline keyboard for a list page.

    `entries` is (token, url, row_number, question) in rendered order. Each
    market gets a button naming it, plus a link button when the market has a
    URL - a row with no URL simply gets no link rather than a dead one.
    """
    rows: list[list[dict]] = []
    for token, url, number, question in entries:
        row = [
            {
                "text": button_label(number, question),
                "callback_data": nav(VIEW_MARKET, token),
            }
        ]
        if url:
            row.append({"text": t("btn.link_short", lang), "url": url})
        rows.append(row)

    if pages > 1:
        paging: list[dict] = []
        if page > 0:
            paging.append({"text": t("btn.prev", lang), "callback_data": nav(view, str(page - 1))})
        paging.append(
            {
                "text": t("list.page", lang, page=page + 1, pages=pages),
                # Re-rendering the page it is already on. Telegram rejects an
                # edit that changes nothing, so `_render` answers this locally
                # rather than letting it surface as an API error.
                "callback_data": nav(view, str(page)),
            }
        )
        if page + 1 < pages:
            paging.append({"text": t("btn.next", lang), "callback_data": nav(view, str(page + 1))})
        rows.append(paging)

    rows.append(nav_row(lang))
    return {"inline_keyboard": rows}


def _side_label(key: str, lang: str, price: float | None) -> str:
    """`Buy YES · 44¢` - the button states what it would cost.

    Without the price the two buy buttons are interchangeable-looking and the
    owner has to scroll back into the text to remember which side is which.
    """
    base = t(key, lang)
    if price is None:
        return base
    return f"{base} · {round(price * 100)}¢"


def market_keyboard(
    token: str,
    url: str | None,
    *,
    lang: str,
    back_view: str,
    yes_price: float | None = None,
    no_price: float | None = None,
    watching: bool = False,
) -> dict:
    """Detail screen: buy either side, watch, open on Polymarket, go back.

    One action per row below the buy pair, each labelled with what it does
    rather than an icon alone - the reported problem with the first version was
    that a column of unlabelled buttons gave no clue what any of them did.
    """
    rows: list[list[dict]] = [
        [
            {
                "text": _side_label("btn.buy_yes", lang, yes_price),
                "callback_data": nav(VIEW_BUY, f"y{token}"),
            },
            {
                "text": _side_label("btn.buy_no", lang, no_price),
                "callback_data": nav(VIEW_BUY, f"n{token}"),
            },
        ],
        [
            {
                "text": t("btn.unwatch" if watching else "btn.watch", lang),
                "callback_data": nav(VIEW_WATCH, token),
            }
        ],
    ]
    if url:
        rows.append([{"text": t("btn.link", lang), "url": url}])
    rows.append(nav_row(lang, back=back_view))
    return {"inline_keyboard": rows}


def confirm_action_keyboard(action: str, *, lang: str) -> dict:
    """Yes/No for a More-menu action that cannot be undone.

    The confirmed payload is the action with a "!" suffix, so the plain form
    can never execute - reaching the action requires the second tap by
    construction rather than by a flag someone can forget to check.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": t("btn.confirm_yes", lang),
                    "callback_data": nav(VIEW_MORE, f"{action}!"),
                },
                {"text": t("btn.confirm_no", lang), "callback_data": nav(VIEW_MORE, "")},
            ]
        ]
    }


def more_keyboard(lang: str) -> dict:
    """The More screen.

    Grouped by what an action DOES, because the first version was an
    undifferentiated grid of buttons: things that only read, then things that
    move money, then settings. The two money-moving actions sit on their own
    row so neither is a neighbour of a harmless one.
    """
    return {
        "inline_keyboard": [
            # Find things.
            [
                {"text": t("btn.arb", lang), "callback_data": nav(VIEW_ARB, "")},
                {"text": t("btn.watchlist", lang), "callback_data": nav(VIEW_WATCHLIST, "")},
            ],
            # Read things.
            [
                {"text": t("btn.status", lang), "callback_data": nav(VIEW_MORE, "status")},
                {"text": t("btn.analyze", lang), "callback_data": nav(VIEW_MORE, "analyze")},
            ],
            [
                {"text": t("btn.rules", lang), "callback_data": nav(VIEW_MORE, "rules")},
                {"text": t("btn.monitor", lang), "callback_data": nav(VIEW_MORE, "monitor")},
            ],
            # Move money. Kept apart from the read-only rows above.
            [
                {"text": t("btn.redeem", lang), "callback_data": nav(VIEW_MORE, "redeem")},
                {"text": t("btn.cancel_orders", lang), "callback_data": nav(VIEW_MORE, "cancel")},
            ],
            # Settings.
            [{"text": t("btn.language", lang), "callback_data": nav(VIEW_LANG, "")}],
            nav_row(lang),
        ]
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _fmt_price(price: float | None, lang: str) -> str:
    if price is None:
        return t("row.unknown", lang)
    return t("row.yes", lang, pct=round(price * 100))


def _fmt_spread(spread: float | None, lang: str) -> str | None:
    if spread is None:
        return None
    return t("row.spread", lang, cents=f"{spread * 100:.1f}")


def _fmt_days(days: int | None, lang: str) -> str:
    if days is None:
        return t("row.days_unknown", lang)
    if days <= 0:
        return t("row.days_today", lang)
    return t("row.days", lang, days=days)


def _fmt_volume(volume: float | None, lang: str) -> str | None:
    if not volume:
        return None
    return t("row.volume", lang, amount=f"${volume:,.0f}")


def render_market_row(index: int, row: dict, lang: str) -> str:
    """One market, as the list shows it.

    One fact per line rather than aligned columns: see the module docstring on
    right-to-left rendering.
    """
    facts = [f for f in (
        _fmt_price((row.get("yes") or {}).get("price"), lang),
        _fmt_spread(row.get("spread"), lang),
        _fmt_days(row.get("days_left"), lang),
    ) if f]
    lines = [f"{index}. {row.get('question') or row.get('slug') or '?'}", "   " + "  ·  ".join(facts)]
    volume = _fmt_volume(row.get("volume_24h"), lang)
    if volume:
        lines.append(f"   {volume}")
    return "\n".join(lines)


def render_list(
    rows: list[dict], *, lang: str, page: int, title: str, note: str | None = None
) -> tuple[str, list[dict], int]:
    """Render one page of markets.

    Returns (text, page_rows, total_pages). The caller mints tokens for
    `page_rows` and builds the keyboard, so this stays free of session state.
    """
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    page_rows = rows[start : start + PAGE_SIZE]

    body = [title, ""]
    for offset, row in enumerate(page_rows, start=start + 1):
        body.append(render_market_row(offset, row, lang))
        body.append("")
    if note:
        body.append(note)
    return ("\n".join(body).rstrip(), page_rows, pages)
