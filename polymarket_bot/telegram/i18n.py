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
    # ---- home dashboard --------------------------------------------------
    "home.title": {"en": "💼 Polymarket", "he": "💼 פולימרקט"},
    "home.cash": {"en": "Cash        {amount}", "he": "מזומן       {amount}"},
    "home.value": {"en": "Total       {amount}", "he": "סה\"כ        {amount}"},
    "home.positions": {"en": "Positions   {count} open", "he": "פוזיציות    {count} פתוחות"},
    "home.pnl": {"en": "Unrealized  {amount}", "he": "רווח פתוח   {amount}"},
    "home.rules": {"en": "Exit rules  {count} armed", "he": "כללי יציאה  {count} פעילים"},
    "home.dry_run": {
        "en": "⚠️ Monitor is in DRY RUN - exit rules are watched, not executed.",
        "he": "⚠️ המוניטור בהרצה יבשה - כללי היציאה נצפים, לא מבוצעים.",
    },
    "home.hint": {
        "en": "Tap Refresh to update the balance.",
        "he": "לחץ רענן כדי לעדכן את היתרה.",
    },
    "msg.keyboard_cleared": {
        "en": "…",
        "he": "…",
    },
    # ---- screen titles ---------------------------------------------------
    "title.hot": {"en": "🔥 Hot markets", "he": "🔥 שווקים חמים"},
    "title.search": {
        "en": "🔍 {count} results for “{query}”",
        "he": "🔍 {count} תוצאות עבור “{query}”",
    },
    "title.portfolio": {"en": "💼 Portfolio", "he": "💼 תיק"},
    "title.more": {"en": "⚙️ More", "he": "⚙️ עוד"},
    "title.market": {"en": "📊 Market", "he": "📊 שוק"},
    "title.watchlist": {"en": "⭐ Watchlist", "he": "⭐ רשימת מעקב"},
    "title.positions": {"en": "💼 Positions", "he": "💼 פוזיציות"},
    # ---- positions -------------------------------------------------------
    "pos.row": {
        "en": "{n}. {title}\n   {outcome}: {shares} sh @ {entry}¢ → {now}¢\n   Value {value}  ·  P&L {pnl} ({pct}%)",
        "he": "{n}. {title}\n   {outcome}: {shares} מניות @ {entry}א ← {now}א\n   שווי {value}  ·  רווח {pnl} ({pct}%)",
    },
    "pos.settled": {
        "en": "   ⚠️ SETTLED - redeem it, it cannot be sold.",
        "he": "   ⚠️ הוכרע - יש לפדות, אי אפשר למכור.",
    },
    "pos.empty": {
        "en": "No open positions.",
        "he": "אין פוזיציות פתוחות.",
    },
    "pos.total": {
        "en": "Total value {value}  ·  unrealized {pnl}",
        "he": "שווי כולל {value}  ·  רווח לא ממומש {pnl}",
    },
    "pos.pick": {
        "en": "Tap a position to sell part or all of it.",
        "he": "לחץ על פוזיציה כדי למכור חלק ממנה או את כולה.",
    },
    "pos.selling": {
        "en": "Pricing a sell of {pct}% ({shares} shares)...",
        "he": "מתמחר מכירה של {pct}% ({shares} מניות)...",
    },
    "title.arb": {"en": "⚖️ Arbitrage scan", "he": "⚖️ סריקת ארביטראז'"},
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
    "btn.link": {"en": "🔗 Open on Polymarket", "he": "🔗 פתח בפולימרקט"},
    # Sits beside a market button, where the row already names the market.
    "btn.link_short": {"en": "🔗", "he": "🔗"},
    "btn.watch": {"en": "⭐ Watch this market", "he": "⭐ עקוב אחרי השוק"},
    "btn.unwatch": {"en": "★ Stop watching", "he": "★ הפסק לעקוב"},
    "btn.watchlist": {"en": "⭐ Watchlist", "he": "⭐ מעקב"},
    "btn.arb": {"en": "⚖️ Arbitrage scan", "he": "⚖️ סריקת ארביטראז'"},
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
    "btn.home": {"en": "🏠 Home", "he": "🏠 ראשי"},
    "btn.settings": {"en": "⚙️ Settings", "he": "⚙️ הגדרות"},
    "btn.sell_all": {"en": "💵 Sell all", "he": "💵 מכור הכל"},
    "btn.sell_half": {"en": "Sell 50%", "he": "מכור 50%"},
    "btn.sell_quarter": {"en": "Sell 25%", "he": "מכור 25%"},
    "btn.market": {"en": "📊 Market details", "he": "📊 פרטי השוק"},
    "btn.confirm_yes": {"en": "✅ Yes, do it", "he": "✅ כן, בצע"},
    "btn.confirm_no": {"en": "✖ No, go back", "he": "✖ לא, חזור"},
    # ---- confirmations for irreversible actions --------------------------
    "confirm.cancel": {
        "en": (
            "Cancel every resting order?\n\n"
            "This also removes any take-profit sitting on the book - the only "
            "exit that keeps working while the bot is offline. It cannot be undone."
        ),
        "he": (
            "לבטל את כל ההזמנות הפתוחות?\n\n"
            "זה גם מוחק כל take-profit שיושב בספר - היחיד שממשיך לעבוד "
            "כשהבוט כבוי. אי אפשר לבטל את הפעולה."
        ),
    },
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
    "msg.position_gone": {
        "en": "That position is no longer open - it may have been sold or settled.",
        "he": "הפוזיציה כבר לא פתוחה - ייתכן שנמכרה או הוכרעה.",
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
    # ---- watchlist -------------------------------------------------------
    "msg.watch_added": {
        "en": "⭐ Added to your watchlist.",
        "he": "⭐ נוסף לרשימת המעקב.",
    },
    "msg.watch_removed": {
        "en": "Removed from your watchlist.",
        "he": "הוסר מרשימת המעקב.",
    },
    "msg.watchlist_empty": {
        "en": "Your watchlist is empty. Open a market and tap ⭐ to add it.",
        "he": "רשימת המעקב ריקה. פתח שוק ולחץ ⭐ כדי להוסיף אותו.",
    },
    # ---- arbitrage -------------------------------------------------------
    "arb.scanning": {
        "en": "Scanning order books for arbitrage. This takes a moment...",
        "he": "סורק ספרי פקודות אחרי ארביטראז'. זה לוקח רגע...",
    },
    "arb.none": {
        "en": (
            "No arbitrage found in {scanned} markets.\n\n"
            "That is the normal result. These gaps are what market makers exist "
            "to close, and they close in seconds."
        ),
        "he": (
            "לא נמצא ארביטראז' ב-{scanned} שווקים.\n\n"
            "זו התוצאה הרגילה. הפערים האלה הם בדיוק מה שעושי שוק סוגרים, "
            "והם נסגרים תוך שניות."
        ),
    },
    "arb.found": {
        "en": "Found {count} possible arbitrage(s) in {scanned} markets:",
        "he": "נמצאו {count} הזדמנויות ארביטראז' אפשריות מתוך {scanned} שווקים:",
    },
    "arb.row": {
        "en": (
            "{n}. {question}\n"
            "   Buy YES {yes}¢ + NO {no}¢ = {total}¢ → pays 100¢\n"
            "   Edge {edge}¢ per pair ({pct}%) on up to {size} pairs\n"
            "   Best case {profit} before fees"
        ),
        "he": (
            "{n}. {question}\n"
            "   קנה כן {yes}א + לא {no}א = {total}א ← משלם 100א\n"
            "   רווח {edge}א לזוג ({pct}%) עד {size} זוגות\n"
            "   מקסימום {profit} לפני עמלות"
        ),
    },
    # ---- maker pairs -----------------------------------------------------
    "maker.title": {
        "en": "🅜 Maker pairs: {count} of {scanned} markets",
        "he": "🅜 זוגות למתווך: {count} מתוך {scanned} שווקים",
    },
    "maker.how": {
        "en": (
            "Post a BUY limit on both sides. If BOTH fill, the YES and NO "
            "redeem together for exactly 100¢ whatever happens."
        ),
        "he": (
            "הצב הזמנת קנייה בשני הצדדים. אם שתיהן מתמלאות, הכן והלא "
            "נפדים יחד בדיוק ב-100 אגורות, לא משנה מה קורה."
        ),
    },
    "maker.row": {
        "en": (
            "{n}. {question}\n"
            "   Bid YES {yes}¢ + NO {no}¢ = {total}¢ → redeems 100¢\n"
            "   Edge {edge}¢/pair, up to {size} pairs = {profit} if both fill"
        ),
        "he": (
            "{n}. {question}\n"
            "   הצע כן {yes}א + לא {no}א = {total}א ← נפדה ב-100א\n"
            "   רווח {edge}א לזוג, עד {size} זוגות = {profit} אם שתיהן מתמלאות"
        ),
    },
    # The rate is the MARKET'S daily pool shared among every liquidity
    # provider, not an individual payout. Wording it as "pays you" would be
    # wildly misleading on a small account.
    "maker.rewards": {
        "en": "💧 Market shares ~{rate}/day among ALL makers; your cut is your share of the size.",
        "he": "💧 השוק מחלק ~{rate} ליום בין כל המתווכים; חלקך לפי חלקך בגודל.",
    },
    "maker.none": {
        "en": "No maker pairs found across {scanned} markets.",
        "he": "לא נמצאו זוגות למתווך ב-{scanned} שווקים.",
    },
    "maker.warning": {
        "en": (
            "THIS IS MARKET MAKING, NOT FREE MONEY.\n"
            "A resting bid trades only when someone crosses it, and it may "
            "never fill. If one leg fills and the other does not, you are "
            "holding a naked directional position. The side that fills first "
            "is disproportionately the side moving against you — that adverse "
            "selection is exactly why this gap exists and why it is this size. "
            "The bot will not place these; use /buy with a limit price to try "
            "one by hand."
        ),
        "he": (
            "זה עשיית שוק, לא כסף חינם.\n"
            "הזמנה ממתינה מתבצעת רק כשמישהו חוצה אותה, וייתכן שלעולם לא "
            "תתמלא. אם רגל אחת מתמלאת והשנייה לא, אתה מחזיק פוזיציה חשופה. "
            "הצד שמתמלא ראשון הוא לרוב הצד שהשוק זז נגדו — הסלקציה הזו היא "
            "בדיוק הסיבה שהפער קיים ושהוא בגודל הזה. הבוט לא יציב את אלה; "
            "השתמש ב-/buy עם מחיר גבול כדי לנסות ידנית."
        ),
    },
    "arb.fees_kill": {
        "en": "Fees eat this one - not a trade.",
        "he": "העמלות אוכלות את זה - לא עסקה.",
    },
    "arb.warning": {
        "en": (
            "READ THIS FIRST. Both legs must fill; if one fills and the other "
            "moves you are left holding a naked position. Taker fees on some "
            "markets reach 5%, which is larger than every edge above. The sizes "
            "shown are what rests on the book right now and will not be there "
            "when you tap. This is a report, not a recommendation - the bot will "
            "not execute these for you."
        ),
        "he": (
            "קרא את זה קודם. שתי הרגליים חייבות להתמלא; אם אחת מתמלאת והשנייה זזה, "
            "נשארת עם פוזיציה חשופה. עמלות בחלק מהשווקים מגיעות ל-5%, יותר מכל "
            "הרווח שלמעלה. הגדלים שמוצגים הם מה שיושב בספר עכשיו ולא יהיה שם "
            "כשתלחץ. זה דוח, לא המלצה - הבוט לא יבצע את זה עבורך."
        ),
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
