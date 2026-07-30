"""Tests for scan's sort modes and the per-row Polymarket link.

`sort="spread"` is the default and must keep behaving exactly as it did, since
every existing caller (the CLI, the old Telegram commands) relies on it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot import service
from polymarket_bot.config import Settings


@pytest.fixture(autouse=True)
def _isolated_settings():
    with mock.patch.object(
        service, "load_settings",
        return_value=Settings(private_key="0x0", wallet="0xw", data_dir=Path(".")),
    ):
        yield


def _market(slug, *, volume, spread, event="ev", question=None):
    return SimpleNamespace(
        slug=slug,
        events=(SimpleNamespace(slug=event),),
        condition_id=f"0x{slug}",
        question=question or f"Will {slug} happen?",
        group_item_title=None,
        state=SimpleNamespace(active=True, closed=False, accepting_orders=True, end_date=None),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(label="Yes", token_id=f"T{slug}", price=0.5),
            no=SimpleNamespace(label="No", token_id=f"N{slug}", price=0.5),
        ),
        metrics=SimpleNamespace(volume_24hr=volume, liquidity_num=1000),
        prices=SimpleNamespace(spread=spread),
        rewards=SimpleNamespace(clob_rewards=(), rewards_min_size=None),
    )


# Deliberately ordered so volume and spread disagree: sorting by the wrong key
# is then visible in the output rather than coincidentally identical.
_MARKETS = [
    _market("low-vol-tight", volume=1_000, spread=0.001),
    _market("high-vol-wide", volume=9_000_000, spread=0.05),
    _market("mid", volume=50_000, spread=0.01),
]


def _run(**kwargs):
    with mock.patch.object(service, "list_tradable_markets", return_value=list(_MARKETS)), \
         mock.patch.object(service, "get_spreads", return_value={}):
        return service.scan(client=SimpleNamespace(), **kwargs)


def test_default_sort_is_spread_tightest_first():
    response = _run()
    assert [m["slug"] for m in response["markets"]] == ["low-vol-tight", "mid", "high-vol-wide"]
    assert response["sort"] == "spread"


def test_hot_sort_puts_the_highest_24h_volume_first():
    response = _run(sort="hot")
    assert [m["slug"] for m in response["markets"]] == ["high-vol-wide", "mid", "low-vol-tight"]
    assert response["sort"] == "hot"


def test_hot_asks_the_api_for_volume_ordering():
    with mock.patch.object(service, "list_tradable_markets", return_value=list(_MARKETS)) as listing, \
         mock.patch.object(service, "get_spreads", return_value={}):
        service.scan(sort="hot", client=SimpleNamespace())
    assert listing.call_args.kwargs["order"] == "volume24hr"
    assert listing.call_args.kwargs["ascending"] is False


def test_spread_sort_does_not_ask_the_api_to_order():
    # The API cannot sort by live spread; asking it to would order by something
    # else entirely while the caller believed it got tightest-first.
    with mock.patch.object(service, "list_tradable_markets", return_value=list(_MARKETS)) as listing, \
         mock.patch.object(service, "get_spreads", return_value={}):
        service.scan(client=SimpleNamespace())
    assert listing.call_args.kwargs["order"] is None


def test_every_row_carries_a_polymarket_url():
    response = _run(sort="hot")
    for row in response["markets"]:
        assert row["url"] == f"https://polymarket.com/event/ev/{row['slug']}"


def test_hot_text_says_volume_not_tradability():
    # The wording is the honesty guard: "hot" must not read as "likely to win".
    response = _run(sort="hot")
    assert "popularity" in response["text"]
    assert "not edge" in response["text"]


def test_spread_text_is_unchanged():
    response = _run()
    assert "Sorted by spread" in response["text"]


def test_keyword_filter_still_applies_under_hot():
    response = _run(sort="hot", keyword="mid")
    assert [m["slug"] for m in response["markets"]] == ["mid"]


def test_unknown_sort_value_falls_back_to_spread():
    response = _run(sort="sideways")
    assert response["sort"] == "spread"
