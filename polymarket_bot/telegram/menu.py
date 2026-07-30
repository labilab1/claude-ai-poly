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

# View names, also used as the `nav:` action segment.
VIEW_HOME = "home"
VIEW_HOT = "hot"
VIEW_SEARCH = "search"
VIEW_MARKET = "mkt"
VIEW_PORTFOLIO = "pf"
VIEW_MORE = "more"
VIEW_LANG = "lang"
VIEW_BUY = "buy"

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
    touched_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.touched_at = time.monotonic()

    def remember(self, market_ref: str) -> str:
        """Mint a short token for a market ref and return it.

        Tokens are per-session and short-lived by design: they only have to
        survive from rendering a list to tapping a row in it.
        """
        token = uuid.uuid4().hex[:8]
        self.refs[token] = market_ref
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


def persistent_keyboard(lang: str) -> dict:
    """The always-visible bottom keyboard. Sent with every screen."""
    return {
        "keyboard": [
            [{"text": t("menu.hot", lang)}, {"text": t("menu.search", lang)}],
            [{"text": t("menu.portfolio", lang)}, {"text": t("menu.more", lang)}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def reply_labels(lang: str) -> dict[str, str]:
    """Map the persistent keyboard's visible text back onto a view.

    The buttons send plain text, not callbacks, so the router has to recognise
    them by label - in whichever language they were rendered in. Both
    languages are always accepted so a toggle mid-session cannot strand the
    keyboard already on screen.
    """
    mapping: dict[str, str] = {}
    for language in ("en", "he"):
        mapping[t("menu.hot", language)] = VIEW_HOT
        mapping[t("menu.search", language)] = VIEW_SEARCH
        mapping[t("menu.portfolio", language)] = VIEW_PORTFOLIO
        mapping[t("menu.more", language)] = VIEW_MORE
    return mapping


def market_rows_keyboard(
    entries: list[tuple[str, str | None]], *, lang: str, page: int, pages: int, view: str
) -> dict:
    """Inline keyboard for a list page.

    `entries` is (token, url) per row, in the order the rows were rendered.
    A row with no URL simply gets no link button rather than a dead one.
    """
    rows: list[list[dict]] = []
    for token, url in entries:
        row = [{"text": t("btn.details", lang), "callback_data": nav(VIEW_MARKET, token)}]
        if url:
            row.append({"text": t("btn.link", lang), "url": url})
        rows.append(row)

    if pages > 1:
        nav_row: list[dict] = []
        if page > 0:
            nav_row.append({"text": t("btn.prev", lang), "callback_data": nav(view, str(page - 1))})
        nav_row.append(
            {
                "text": t("list.page", lang, page=page + 1, pages=pages),
                # A no-op target: the label is information, but Telegram
                # requires every inline button to carry an action.
                "callback_data": nav(view, str(page)),
            }
        )
        if page + 1 < pages:
            nav_row.append({"text": t("btn.next", lang), "callback_data": nav(view, str(page + 1))})
        rows.append(nav_row)

    return {"inline_keyboard": rows}


def market_keyboard(token: str, url: str | None, *, lang: str, back_view: str) -> dict:
    """Detail screen: buy either side, open on Polymarket, go back."""
    rows: list[list[dict]] = [
        [
            {"text": t("btn.buy_yes", lang), "callback_data": nav(VIEW_BUY, f"y{token}")},
            {"text": t("btn.buy_no", lang), "callback_data": nav(VIEW_BUY, f"n{token}")},
        ]
    ]
    if url:
        rows.append([{"text": t("btn.link", lang), "url": url}])
    rows.append([{"text": t("btn.back", lang), "callback_data": nav(back_view, "0")}])
    return {"inline_keyboard": rows}


def more_keyboard(lang: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": t("btn.status", lang), "callback_data": nav(VIEW_MORE, "status")},
                {"text": t("btn.rules", lang), "callback_data": nav(VIEW_MORE, "rules")},
            ],
            [
                {"text": t("btn.analyze", lang), "callback_data": nav(VIEW_MORE, "analyze")},
                {"text": t("btn.monitor", lang), "callback_data": nav(VIEW_MORE, "monitor")},
            ],
            [
                {"text": t("btn.redeem", lang), "callback_data": nav(VIEW_MORE, "redeem")},
                {"text": t("btn.cancel_orders", lang), "callback_data": nav(VIEW_MORE, "cancel")},
            ],
            [{"text": t("btn.language", lang), "callback_data": nav(VIEW_LANG, "")}],
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
