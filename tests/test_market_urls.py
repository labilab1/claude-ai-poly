"""Tests for Polymarket link building and hot-first market ordering.

Both URL shapes below were verified against the live site (2026-07-30):
`/market/<market-slug>` returns 200 and redirects to the canonical
`/event/<event-slug>/<market-slug>`, which also returns 200. The canonical
form is built directly when the market carries an event, so the user's tap
does not pay for a redirect.
"""

from __future__ import annotations

from types import SimpleNamespace

from polymarket_bot.markets import list_tradable_markets, market_url


def _market(slug="will-x-happen", event_slug="x-event", *, accepting=True, token="TOK"):
    events = (SimpleNamespace(slug=event_slug),) if event_slug else ()
    return SimpleNamespace(
        slug=slug,
        events=events,
        condition_id="0xc",
        question="Will X happen?",
        state=SimpleNamespace(active=True, closed=False, accepting_orders=accepting),
        outcomes=SimpleNamespace(yes=SimpleNamespace(token_id=token)),
    )


# ---------------------------------------------------------------------------
# market_url
# ---------------------------------------------------------------------------


def test_canonical_url_uses_the_event_slug():
    url = market_url(_market(slug="will-btc-hit-100k", event_slug="btc-price"))
    assert url == "https://polymarket.com/event/btc-price/will-btc-hit-100k"


def test_market_without_an_event_falls_back_to_the_market_path():
    # /market/<slug> is a real route; it redirects to the canonical form.
    url = market_url(_market(slug="will-btc-hit-100k", event_slug=None))
    assert url == "https://polymarket.com/market/will-btc-hit-100k"


def test_market_with_no_slug_has_no_url_rather_than_a_broken_one():
    # A link to /market/ or /event// is worse than no link: it looks like a
    # real button and lands on a 404.
    assert market_url(_market(slug=None, event_slug="e")) is None
    assert market_url(_market(slug="", event_slug=None)) is None


def test_an_event_without_a_slug_is_treated_as_no_event():
    url = market_url(_market(slug="the-market", event_slug=""))
    assert url == "https://polymarket.com/market/the-market"


def test_url_building_never_raises_on_an_odd_market_object():
    # Market models change; a missing attribute must cost a link, not a screen.
    assert market_url(SimpleNamespace()) is None


# ---------------------------------------------------------------------------
# ordering passthrough
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, markets):
        self._markets = markets
        self.calls: list[dict] = []

    def list_markets(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(iter_items=lambda: iter(self._markets))


def test_ordering_arguments_reach_the_sdk():
    client = _FakeClient([_market()])
    list_tradable_markets(client, limit=5, order="volume24hr", ascending=False)
    assert client.calls[0]["order"] == "volume24hr"
    assert client.calls[0]["ascending"] is False


def test_no_ordering_arguments_are_sent_when_none_is_asked_for():
    """Existing callers must keep their exact behaviour: passing order=None
    through would be a different request than omitting it."""
    client = _FakeClient([_market()])
    list_tradable_markets(client, limit=5)
    assert "order" not in client.calls[0]
    assert "ascending" not in client.calls[0]


def test_the_limit_still_bounds_the_walk():
    # iter_items() pages through the entire result set; an unbounded walk here
    # hangs the bot. The break is the bound.
    client = _FakeClient([_market(slug=f"m{i}") for i in range(50)])
    out = list_tradable_markets(client, limit=3)
    assert len(out) == 3


def test_untradable_markets_are_skipped():
    client = _FakeClient([_market(slug="closed", accepting=False), _market(slug="open")])
    out = list_tradable_markets(client, limit=5)
    assert [m.slug for m in out] == ["open"]
