"""Tests for the menu's view state, keyboards and rendering.

The two properties worth guarding hardest:

  * No callback payload may exceed Telegram's 64-byte cap. Over the cap
    Telegram truncates, and a truncated payload navigates somewhere the user
    did not tap - a silent, miserable bug.
  * Sessions must stay bounded. A long-running bot with an unbounded session
    dict leaks for as long as it runs.
"""

from __future__ import annotations

import time

import pytest

from polymarket_bot.telegram import menu
from polymarket_bot.telegram.menu import (
    PAGE_SIZE,
    PAYLOAD_LIMIT,
    MenuSession,
    SessionStore,
    market_keyboard,
    market_rows_keyboard,
    more_keyboard,
    nav,
    parse_nav,
    persistent_keyboard,
    render_list,
    render_market_row,
    reply_labels,
)

# A real slug from the live API - 58 characters, which is why slugs cannot
# ride inside callback_data.
LONG_SLUG = "will-adanech-abiebie-be-the-next-prime-minister-of-ethiopia"


def _row(slug="market-a", *, price=0.62, spread=0.012, days=14, volume=1_752_417):
    return {
        "slug": slug,
        "question": "Will there be no change in Fed interest rates?",
        "yes": {"label": "Yes", "price": price},
        "spread": spread,
        "days_left": days,
        "volume_24h": volume,
        "url": f"https://polymarket.com/event/ev/{slug}",
    }


# ---------------------------------------------------------------------------
# callback payload budget
# ---------------------------------------------------------------------------


def test_a_token_payload_fits_the_limit():
    session = MenuSession()
    token = session.remember(LONG_SLUG)
    assert len(nav(menu.VIEW_MARKET, token).encode()) <= PAYLOAD_LIMIT


def test_building_an_oversized_payload_raises_rather_than_being_truncated():
    with pytest.raises(ValueError):
        nav(menu.VIEW_MARKET, LONG_SLUG * 2)


def test_a_raw_slug_would_not_have_fit_which_is_why_tokens_exist():
    # Guards the reason for the indirection, so nobody "simplifies" it away.
    raw = f"nav:{menu.VIEW_MARKET}:{LONG_SLUG}"
    assert len(raw.encode()) > PAYLOAD_LIMIT


def test_every_generated_keyboard_payload_is_within_the_limit():
    session = MenuSession()
    entries = [
        (session.remember(f"{LONG_SLUG}-{i}"), "https://polymarket.com/x", i + 1, f"Market {i}")
        for i in range(5)
    ]
    keyboards = [
        market_rows_keyboard(entries, lang="he", page=1, pages=9, view=menu.VIEW_HOT),
        market_keyboard(entries[0][0], "https://polymarket.com/x", lang="he", back_view=menu.VIEW_HOT),
        more_keyboard("he"),
    ]
    for keyboard in keyboards:
        for row in keyboard["inline_keyboard"]:
            for button in row:
                data = button.get("callback_data")
                if data is not None:
                    assert len(data.encode()) <= PAYLOAD_LIMIT, f"{data!r} too long"


def test_parse_nav_round_trips():
    assert parse_nav("hot:3") == ("hot", "3")
    assert parse_nav("home:") == ("home", "")


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_tokens_resolve_back_to_their_market_ref():
    session = MenuSession()
    token = session.remember(LONG_SLUG)
    assert session.resolve(token) == LONG_SLUG


def test_an_unknown_token_resolves_to_none_rather_than_raising():
    assert MenuSession().resolve("nope") is None


def test_two_markets_get_distinct_tokens():
    session = MenuSession()
    assert session.remember("a") != session.remember("b")


def test_idle_sessions_are_evicted():
    store = SessionStore(ttl_seconds=0.05)
    store.get(1)
    assert len(store) == 1
    time.sleep(0.08)
    store.get(2)
    assert len(store) == 1, "an idle session survived past its TTL"


def test_session_count_is_capped():
    store = SessionStore(max_sessions=3)
    for chat_id in range(10):
        store.get(chat_id)
    assert len(store) <= 3


def test_the_same_chat_keeps_its_session_across_calls():
    store = SessionStore()
    store.get(7).page = 4
    assert store.get(7).page == 4


def test_clear_prompt_drops_every_pending_field():
    session = MenuSession()
    session.awaiting = menu.AWAIT_BUY_AMOUNT
    session.pending_market = "m"
    session.pending_outcome = "yes"
    session.clear_prompt()
    assert (session.awaiting, session.pending_market, session.pending_outcome) == (None, None, None)


# ---------------------------------------------------------------------------
# keyboards
# ---------------------------------------------------------------------------


def test_persistent_keyboard_has_all_four_entries_in_the_chosen_language():
    keyboard = persistent_keyboard("he")
    labels = [b["text"] for row in keyboard["keyboard"] for b in row]
    assert len(labels) == 4
    assert any("חם" in label for label in labels)


def test_reply_labels_accept_both_languages():
    """A language toggle does not repaint the keyboard already on screen, so
    the router must still recognise labels in the previous language."""
    labels = reply_labels("en")
    from polymarket_bot.telegram.i18n import t

    assert labels[t("menu.hot", "en")] == menu.VIEW_HOT
    assert labels[t("menu.hot", "he")] == menu.VIEW_HOT


def test_a_row_without_a_url_gets_no_link_button():
    keyboard = market_rows_keyboard([("tok", None, 1, "A market")], lang="en", page=0, pages=1, view=menu.VIEW_HOT)
    buttons = keyboard["inline_keyboard"][0]
    assert len(buttons) == 1 and "url" not in buttons[0]


def test_pagination_row_is_absent_on_a_single_page():
    keyboard = market_rows_keyboard([("tok", None, 1, "A market")], lang="en", page=0, pages=1, view=menu.VIEW_HOT)
    assert len(keyboard["inline_keyboard"]) == 1


def test_first_page_has_next_but_no_prev():
    keyboard = market_rows_keyboard([("tok", None, 1, "A market")], lang="en", page=0, pages=3, view=menu.VIEW_HOT)
    texts = [b["text"] for b in keyboard["inline_keyboard"][-1]]
    assert not any("Prev" in x for x in texts)
    assert any("Next" in x for x in texts)


def test_last_page_has_prev_but_no_next():
    keyboard = market_rows_keyboard([("tok", None, 1, "A market")], lang="en", page=2, pages=3, view=menu.VIEW_HOT)
    texts = [b["text"] for b in keyboard["inline_keyboard"][-1]]
    assert any("Prev" in x for x in texts)
    assert not any("Next" in x for x in texts)


def test_buy_buttons_encode_which_side_was_tapped():
    keyboard = market_keyboard("abc123", None, lang="en", back_view=menu.VIEW_HOT)
    payloads = [b["callback_data"] for b in keyboard["inline_keyboard"][0]]
    assert payloads == [f"nav:{menu.VIEW_BUY}:yabc123", f"nav:{menu.VIEW_BUY}:nabc123"]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_a_row_shows_price_spread_days_and_volume():
    text = render_market_row(1, _row(), "en")
    assert "62" in text and "1.2" in text and "14" in text and "1,752,417" in text


def test_a_row_survives_every_field_being_missing():
    text = render_market_row(1, {"slug": "s"}, "en")
    assert "s" in text  # falls back to the slug, does not raise


def test_hebrew_rows_render_in_hebrew():
    text = render_market_row(1, _row(), "he")
    assert "כן" in text and "מחזור" in text


def test_a_market_ending_today_says_so_rather_than_zero_days():
    assert "today" in render_market_row(1, _row(days=0), "en")


def test_pagination_splits_by_page_size():
    rows = [_row(f"m{i}") for i in range(12)]
    _text, page_rows, pages = render_list(rows, lang="en", page=0, title="T")
    assert len(page_rows) == PAGE_SIZE
    assert pages == 3


def test_a_page_beyond_the_end_clamps_to_the_last_page():
    rows = [_row(f"m{i}") for i in range(6)]
    _text, page_rows, pages = render_list(rows, lang="en", page=99, title="T")
    assert pages == 2 and len(page_rows) == 1


def test_row_numbering_continues_across_pages():
    rows = [_row(f"m{i}") for i in range(12)]
    text, _rows, _pages = render_list(rows, lang="en", page=1, title="T")
    assert "6." in text and "1." not in text.split("\n")[2]


def test_an_empty_list_renders_without_raising():
    text, page_rows, pages = render_list([], lang="en", page=0, title="T")
    assert page_rows == [] and pages == 1 and "T" in text
