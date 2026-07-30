"""Tests for mechanical YES/NO arbitrage detection.

The failure mode that matters is a FALSE POSITIVE: reporting an edge that is
not really there costs real money when someone acts on it. So most of these
pin the conditions under which an apparent edge must be rejected - unreadable
books, dust quotes, midpoints mistaken for asks, and edges smaller than the
fees that would eat them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymarket_bot import arbitrage
from polymarket_bot.arbitrage import (
    DEFAULT_TAKER_FEE,
    MIN_EDGE,
    ArbOpportunity,
    best_ask,
    evaluate_market,
    find_arbitrage,
)


def _book(asks, bids=((0.10, 500),)):
    """Asks DESCENDING, as Polymarket returns them: best (lowest) is last."""
    level = lambda p, s: SimpleNamespace(price=p, size=s)  # noqa: E731
    return SimpleNamespace(
        asks=[level(p, s) for p, s in sorted(asks, key=lambda x: -x[0])],
        bids=[level(p, s) for p, s in sorted(bids, key=lambda x: x[0])],
    )


def _market(slug="m", *, yes_price=0.5, no_price=0.5, accepting=True, min_order=5):
    return SimpleNamespace(
        condition_id="0xc", slug=slug, question=f"Will {slug}?",
        events=(SimpleNamespace(slug="ev"),),
        state=SimpleNamespace(active=True, closed=False, accepting_orders=accepting),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(label="Yes", token_id="TY", price=yes_price),
            no=SimpleNamespace(label="No", token_id="TN", price=no_price),
        ),
        trading=SimpleNamespace(minimum_order_size=min_order),
    )


class _Client:
    def __init__(self, yes_book, no_book, *, raises=False):
        self._books = {"TY": yes_book, "TN": no_book}
        self._raises = raises
        self.reads = 0

    def get_order_book(self, *, token_id):
        self.reads += 1
        if self._raises:
            raise RuntimeError("book unavailable")
        return self._books[token_id]


# ---------------------------------------------------------------------------
# best_ask
# ---------------------------------------------------------------------------


def test_best_ask_is_the_lowest_price_not_the_last_element():
    # Polymarket sorts asks descending. Reading by position instead of by price
    # is the documented trap this guards.
    price, size = best_ask(_book(asks=[(0.60, 100), (0.45, 200), (0.90, 50)]))
    assert price == 0.45 and size == 200


def test_dust_levels_are_ignored():
    """A 1-share quote at a great price is not a price you can trade a pair
    against. Sizing off one manufactures an opportunity that vanishes."""
    price, size = best_ask(_book(asks=[(0.10, 1), (0.48, 400)]))
    assert price == 0.48 and size == 400


def test_an_empty_book_has_no_ask():
    assert best_ask(_book(asks=[])) == (None, 0.0)


def test_a_malformed_level_is_skipped_not_fatal():
    book = SimpleNamespace(asks=[SimpleNamespace(price="oops", size="x"), SimpleNamespace(price=0.4, size=100)])
    assert best_ask(book) == (0.4, 100)


def test_prices_outside_zero_to_one_are_rejected():
    # A price of 0 or >1 means the units are not what we think they are.
    assert best_ask(_book(asks=[(0.0, 100), (1.5, 100)])) == (None, 0.0)


# ---------------------------------------------------------------------------
# evaluate_market: finding a real edge
# ---------------------------------------------------------------------------


def test_a_genuine_underpriced_pair_is_found():
    client = _Client(_book([(0.45, 300)]), _book([(0.50, 300)]))
    opportunity = evaluate_market(client, _market())
    assert opportunity is not None
    assert opportunity.cost == pytest.approx(0.95)
    assert opportunity.gross_edge == pytest.approx(0.05)
    assert opportunity.size_pairs == 300


def test_the_thinner_leg_caps_the_size():
    client = _Client(_book([(0.45, 1000)]), _book([(0.50, 120)]))
    opportunity = evaluate_market(client, _market())
    assert opportunity.size_pairs == 120


def test_a_fairly_priced_pair_is_not_an_opportunity():
    client = _Client(_book([(0.50, 300)]), _book([(0.50, 300)]))
    assert evaluate_market(client, _market()) is None


def test_an_overpriced_pair_is_not_an_opportunity():
    client = _Client(_book([(0.55, 300)]), _book([(0.52, 300)]))
    assert evaluate_market(client, _market()) is None


def test_an_edge_below_the_noise_floor_is_rejected():
    # 0.2c of "edge" is tick rounding, not money.
    client = _Client(_book([(0.499, 300)]), _book([(0.499, 300)]))
    assert evaluate_market(client, _market()) is None


def test_the_threshold_boundary_behaves():
    cheap = round((1.0 - MIN_EDGE) / 2, 4)
    client = _Client(_book([(cheap - 0.01, 300)]), _book([(cheap, 300)]))
    assert evaluate_market(client, _market()) is not None


# ---------------------------------------------------------------------------
# evaluate_market: refusing to guess
# ---------------------------------------------------------------------------


def test_an_unreadable_book_is_skipped_not_treated_as_free_money():
    client = _Client(_book([(0.45, 300)]), _book([(0.50, 300)]), raises=True)
    assert evaluate_market(client, _market()) is None


def test_a_market_not_accepting_orders_is_skipped():
    client = _Client(_book([(0.45, 300)]), _book([(0.50, 300)]))
    assert evaluate_market(client, _market(accepting=False)) is None


def test_a_market_missing_a_token_is_skipped():
    market = _market()
    market.outcomes.no.token_id = None
    client = _Client(_book([(0.45, 300)]), _book([(0.50, 300)]))
    assert evaluate_market(client, market) is None


def test_a_one_sided_book_is_skipped():
    client = _Client(_book([(0.45, 300)]), _book(asks=[]))
    assert evaluate_market(client, _market()) is None


def test_gamma_midpoints_are_never_used_as_the_price():
    """The market quotes 0.40/0.40 (sums to 0.80, looks like a huge edge) but
    the real asks sum to 1.02. The book must win, or every scan is fiction."""
    client = _Client(_book([(0.51, 300)]), _book([(0.51, 300)]))
    assert evaluate_market(client, _market(yes_price=0.40, no_price=0.40)) is None


# ---------------------------------------------------------------------------
# fees
# ---------------------------------------------------------------------------


def test_fees_are_subtracted_from_the_edge():
    client = _Client(_book([(0.45, 300)]), _book([(0.50, 300)]))
    opportunity = evaluate_market(client, _market(), fee_rate=0.02)
    assert opportunity.gross_edge == pytest.approx(0.05)
    assert opportunity.net_edge < opportunity.gross_edge
    assert opportunity.net_edge == pytest.approx(0.05 - 0.95 * 0.02)


def test_a_five_percent_fee_kills_a_small_edge_and_says_so():
    # The honest case: a 2c edge is not an opportunity at a 5% taker fee.
    client = _Client(_book([(0.49, 300)]), _book([(0.49, 300)]))
    opportunity = evaluate_market(client, _market(), fee_rate=0.05)
    assert opportunity is not None, "should still be reported"
    assert opportunity.survives_fees is False
    assert opportunity.net_profit < 0


def test_a_large_edge_survives_a_normal_fee():
    client = _Client(_book([(0.40, 300)]), _book([(0.45, 300)]))
    opportunity = evaluate_market(client, _market(), fee_rate=DEFAULT_TAKER_FEE)
    assert opportunity.survives_fees is True


def test_profit_scales_with_available_size():
    client = _Client(_book([(0.45, 200)]), _book([(0.50, 200)]))
    opportunity = evaluate_market(client, _market(), fee_rate=0.0)
    assert opportunity.gross_profit == pytest.approx(0.05 * 200)


# ---------------------------------------------------------------------------
# warnings
# ---------------------------------------------------------------------------


def test_uneven_legs_are_warned_about():
    client = _Client(_book([(0.45, 1000)]), _book([(0.50, 100)]))
    opportunity = evaluate_market(client, _market())
    assert any("uneven" in w.lower() for w in opportunity.warnings)


def test_a_size_below_the_exchange_minimum_is_warned_about():
    client = _Client(_book([(0.45, 8)]), _book([(0.50, 8)]))
    opportunity = evaluate_market(client, _market(min_order=50))
    assert any("minimum" in w.lower() for w in opportunity.warnings)


# ---------------------------------------------------------------------------
# find_arbitrage
# ---------------------------------------------------------------------------


class _MultiClient:
    def __init__(self, books):
        self._books = books

    def get_order_book(self, *, token_id):
        return self._books[token_id]


def test_scanning_bounds_the_number_of_books_it_reads():
    """Each candidate costs two book reads. An unbounded scan is minutes of
    network I/O for what is meant to be a chat command."""
    markets = [_market(f"m{i}") for i in range(100)]
    reads = {"n": 0}

    class _Counting:
        def get_order_book(self, *, token_id):
            reads["n"] += 1
            return _book([(0.50, 300)])

    _found, priced = find_arbitrage(_Counting(), markets, max_books=10)
    assert priced == 10
    assert reads["n"] <= 20  # two per market


def test_results_are_sorted_by_net_edge():
    class _Varying:
        def __init__(self):
            self.calls = 0

        def get_order_book(self, *, token_id):
            # First market cheap (big edge), second less so.
            self.calls += 1
            return _book([(0.30, 300)]) if self.calls <= 2 else _book([(0.47, 300)])

    found, _priced = find_arbitrage(_Varying(), [_market("a"), _market("b")])
    assert len(found) == 2
    assert found[0].net_edge >= found[1].net_edge


def test_the_priced_count_reflects_work_done_not_markets_supplied():
    markets = [_market(f"m{i}") for i in range(50)]

    class _Flat:
        def get_order_book(self, *, token_id):
            return _book([(0.50, 300)])

    _found, priced = find_arbitrage(_Flat(), markets, max_books=5)
    assert priced == 5, "reporting 50 after pricing 5 overstates how hard it looked"


def test_the_prefilter_lets_an_unquoted_market_through_to_the_book():
    # No gamma quote must mean "let the book decide", not "skip".
    market = _market()
    market.outcomes.yes.price = None
    market.outcomes.no.price = None

    class _Cheap:
        def get_order_book(self, *, token_id):
            return _book([(0.45, 300)])

    found, priced = find_arbitrage(_Cheap(), [market])
    assert priced == 1 and len(found) == 1


def test_an_empty_market_list_is_not_an_error():
    found, priced = find_arbitrage(_MultiClient({}), [])
    assert found == [] and priced == 0


# ---------------------------------------------------------------------------
# the dataclass contract
# ---------------------------------------------------------------------------


def test_to_dict_is_json_safe_and_complete():
    import json

    opportunity = ArbOpportunity(
        condition_id="0xc", slug="s", question="q?", url="https://x",
        yes_price=0.45, no_price=0.50, size_pairs=100, fee_rate=0.02,
    )
    payload = opportunity.to_dict()
    json.dumps(payload)  # must not raise
    for key in ("cost", "gross_edge", "net_edge", "size_pairs", "survives_fees"):
        assert key in payload
