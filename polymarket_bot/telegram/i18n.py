"""UI strings for the Telegram menu, in English and Hebrew.

Scope, decided deliberately: this covers the menu *chrome* - buttons, screen
titles, market rows, prompts, menu-level errors. It does NOT cover the
reports `service.py` renders (the analytics tables, and every warning and
blocker string on a trade preview). Those are safety-critical text where a
mistranslation misleads about money, so they stay in one language until
translating them can be done and reviewed properly.

Two rules keep the table honest:

  * A key must define every language. `tests/test_i18n.py` fails on a key
    that defines only one, because the failure mode otherwise is an English
    string sitting unnoticed in a Hebrew menu.
  * Placeholders must match across languages, or one of them raises at
    format time (or worse, renders a stray brace) the first time it is used.

Hebrew is right-to-left. Rows here are deliberately one fact per line rather
than the fixed-width aligned tables `scripts/_common.py` builds: column
alignment scrambles under bidi, and a table that lines up in English becomes
unreadable in Hebrew.
"""

from __future__ import annotations

import json
import os
from typing import Final
from uuid import uuid4

from polymarket_bot.config import Settings

#: Supported languages. The first is the default and the fallback.
LANGUAGES: Final[tuple[str, ...]] = ("en", "he")
DEFAULT_LANG: Final[str] = LANGUAGES[0]

_PREFS_FILENAME: Final[str] = "telegram_prefs.json"


_STRINGS: Final[dict[str, dict[str, str]]] = {
    # ---- persistent keyboard -------------------------------------------
    "menu.hot": {"en": "🔥 Hot", "he": "🔥 חם"},
    "menu.search": {"en": "🔍 Search", "he": "🔍 חיפוש"},
    "menu.portfolio": {"en": "💼 Portfolio", "he": "💼 תיק"},
    "menu.more": {"en": "⚙️ More", "he": "⚙️ עוד"},
    # ---- screen titles ---------------------------------------------------
    "title.hot": {"en": "🔥 Hot markets", "he": "🔥 שווקים חמים"},
    "title.search": {"en": "🔍 Results for {query}", "he": "🔍 תוצאות עבור {query}"},
    "title.portfolio": {"en": "💼 Portfolio", "he": "💼 תיק"},
    "title.more": {"en": "⚙️ More", "he": "⚙️ עוד"},
    "title.market": {"en": "📊 Market", "he": "📊 שוק"},
    # ---- market rows -----------------------------------------------------
    "row.yes": {"en": "YES {pct}%", "he": "כן {pct}%"},
    "row.spread": {"en": "spread {cents}c", "he": "מרווח {cents}ס"},
    "row.days": {"en": "{days}d left", "he": "{days} ימים"},
    "row.days_today": {"en": "ends today", "he": "מסתיים היום"},
    "row.days_unknown": {"en": "no end date", "he": "אין תאריך סיום"},
    "row.volume": {"en": "24h volume: {amount}", "he": "מחזור 24ש: {amount}"},
    "row.unknown": {"en": "?", "he": "?"},
    # ---- buttons ---------------------------------------------------------
    "btn.details": {"en": "📊 Details", "he": "📊 פרטים"},
    "btn.link": {"en": "🔗 Polymarket", "he": "🔗 פולימרקט"},
    "btn.prev": {"en": "◀ Prev", "he": "◀ הקודם"},
    "btn.next": {"en": "Next ▶", "he": "הבא ▶"},
    "btn.back": {"en": "◀ Back", "he": "◀ חזרה"},
    "btn.refresh": {"en": "🔄 Refresh", "he": "🔄 רענן"},
    "btn.buy_yes": {"en": "Buy YES", "he": "קנה כן"},
    "btn.buy_no": {"en": "Buy NO", "he": "קנה לא"},
    "btn.language": {"en": "🌐 עברית", "he": "🌐 English"},
    "btn.status": {"en": "📈 Status", "he": "📈 מצב"},
    "btn.rules": {"en": "🎯 Exit rules", "he": "🎯 כללי יציאה"},
    "btn.analyze": {"en": "📉 My record", "he": "📉 הביצועים שלי"},
    "btn.monitor": {"en": "👁 Monitor", "he": "👁 מוניטור"},
    "btn.redeem": {"en": "💰 Redeem", "he": "💰 פדיון"},
    "btn.cancel_orders": {"en": "🚫 Cancel orders", "he": "🚫 בטל הזמנות"},
    "btn.help": {"en": "❓ Help", "he": "❓ עזרה"},
    # ---- pagination / lists ---------------------------------------------
    "list.page": {"en": "page {page}/{pages}", "he": "עמוד {page}/{pages}"},
    "list.empty": {
        "en": "Nothing to show here.",
        "he": "אין מה להציג כאן.",
    },
    "list.search_empty": {
        "en": "No market matched {query} in the markets scanned. Try a shorter or more common word.",
        "he": "לא נמצא שוק שמתאים ל-{query} בשווקים שנסרקו. נסה מילה קצרה או נפוצה יותר.",
    },
    # ---- prompts ---------------------------------------------------------
    "prompt.search": {
        "en": "Send a keyword to search for (for example: Fed, Bitcoin, election).",
        "he": "שלח מילת מפתח לחיפוש (למשל: Fed, Bitcoin, בחירות).",
    },
    "prompt.buy_amount": {
        "en": "How much USDC do you want to spend on {outcome}? Send a number (for example: 2.50).",
        "he": "כמה USDC להשקיע ב-{outcome}? שלח מספר (למשל: 2.50).",
    },
    "prompt.bad_amount": {
        "en": "{value} is not a valid amount. Send a number like 2.50.",
        "he": "{value} אינו סכום תקין. שלח מספר כמו 2.50.",
    },
    # ---- status / errors -------------------------------------------------
    "msg.loading": {"en": "Loading...", "he": "טוען..."},
    "msg.welcome": {
        "en": "Polymarket bot. Use the buttons below to browse markets.",
        "he": "בוט פולימרקט. השתמש בכפתורים למטה כדי לעיין בשווקים.",
    },
    "msg.language_set": {
        "en": "Language set to English.",
        "he": "השפה שונתה לעברית.",
    },
    "msg.expired": {
        "en": "This menu is out of date - the bot restarted. Tap a button below to start again.",
        "he": "התפריט הזה כבר לא עדכני - הבוט הופעל מחדש. לחץ על כפתור למטה כדי להתחיל מחדש.",
    },
    "msg.error": {
        "en": "Something went wrong: {error}",
        "he": "משהו השתבש: {error}",
    },
    "msg.hot_note": {
        "en": "Sorted by 24h volume - how much money moved, not how likely it is to pay.",
        "he": "ממוין לפי מחזור 24 שעות - כמה כסף עבר, לא כמה סביר שירוויח.",
    },
}


def _normalise(lang: str | None) -> str:
    """Any unknown or corrupted value degrades to the default.

    A stored preference is user data that can be edited by hand; a bad value
    must not raise on every screen.
    """
    return lang if lang in LANGUAGES else DEFAULT_LANG


def t(key: str, lang: str, **params: object) -> str:
    """Translate `key` into `lang`, formatting any placeholders.

    Raises KeyError for an unknown key on purpose: returning the key would
    ship "menu.nope" to a user as though it were a sentence.
    """
    try:
        translations = _STRINGS[key]
    except KeyError:
        raise KeyError(f"No UI string named {key!r}.") from None
    text = translations.get(_normalise(lang)) or translations[DEFAULT_LANG]
    return text.format(**params) if params else text


def toggled(lang: str) -> str:
    """The other language. With two languages this is the whole toggle."""
    current = _normalise(lang)
    index = LANGUAGES.index(current)
    return LANGUAGES[(index + 1) % len(LANGUAGES)]


# --------------------------------------------------------------------------
# preference storage
# --------------------------------------------------------------------------


def _prefs_path(settings: Settings):
    return settings.data_dir / _PREFS_FILENAME


def _read_prefs(settings: Settings) -> dict:
    """Never raises. A menu that cannot render because a settings file is
    corrupt is worse than a menu in the default language."""
    try:
        raw = _prefs_path(settings).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_lang(settings: Settings, chat_id: int | str) -> str:
    entry = _read_prefs(settings).get(str(chat_id))
    if not isinstance(entry, dict):
        return DEFAULT_LANG
    return _normalise(entry.get("lang"))


def save_lang(settings: Settings, chat_id: int | str, lang: str) -> None:
    """Persist one chat's language, preserving every other chat's entry.

    Read-modify-write rather than overwrite: the file is keyed by chat id, and
    rewriting it from a single entry would silently drop the others.
    """
    settings.ensure_data_dir()
    prefs = _read_prefs(settings)
    entry = prefs.get(str(chat_id))
    if not isinstance(entry, dict):
        entry = {}
    entry["lang"] = _normalise(lang)
    prefs[str(chat_id)] = entry

    # Atomic write, same shape as monitor._save_state: a crash mid-write must
    # not leave a truncated preferences file behind.
    path = _prefs_path(settings)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid4().hex[:6]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(prefs, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
