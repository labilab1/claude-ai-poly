"""Tests for maker-side pair detection (bid(YES) + bid(NO) < $1).

Measured live on 2026-07-31: 23 of 23 liquid markets showed a positive maker
edge, median 1.00c, while ZERO showed a taker edge. So this finder returns
results constantly, and that is exactly why its honesty matters more than the
taker one's: a screen full of "opportunities" that are really unfilled resting
orders would be actively misleading.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymarket_bot.arbitrage import (
    MakerPair,
    best_bid,
    evaluate_maker_pair,
    find_maker_pairs,
)


def _book(bids=(), asks=()):
    level = lambda p, s: SimpleNamespace(price=p, size=s)  # noqa: E731
    return SimpleNamespace(
        bids=[level(p, s) for p, s in sorted(bids, key=lambda x: x[0])],   # ASC
        asks=[level(p, s) for p, s in sorted(asks, key=lambda x: -x[0])],  # DESC
    )


def _market(slug="m", *, accepting=True, reward=0.0):
    rewards = ()
    if reward:
        rewards = (SimpleNamespace(rewards_daily_rate=reward),)
    return SimpleNamespace(
        condition_id="0xc", slug=slug, question=f"Will {slug}?",
        events=(SimpleNamespace(slug="ev"),),
        state=SimpleNamespace(active=True, closed=False, accepting_orders=accepting),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(label="Yes", token_id="TY", price=0.5),
            no=SimpleNamespace(label="No", token_id="TN", price=0.5),
        ),
        trading=SimpleNamespace(minimum_order_size=5),
        rewards=SimpleNamespace(clob_rewards=rewards, rewards_min_size=None),
    )


class _Client:
    def __init__(self, yes_book, no_book, *, raises=False):
        self._books = {"TY": yes_book, "TN": no_book}
        self._raises = raises

    def get_order_book(self, *, token_id):
        if self._raises:
            raise RuntimeError("down")
        return self._books[token_id]


# ---------------------------------------------------------------------------
# best_bid
# ---------------------------------------------------------------------------


def test_best_bid_is_the_highest_price():
    # Bids sort ASCENDING on Polymarket - the mirror of the asks trap.
    price, size = best_bid(_book(bids=[(0.40, 100), (0.49, 200), (0.10, 900)]))
    assert price == 0.49 and size == 200


def test_dust_bids_are_ignored():
    price, size = best_bid(_book(bids=[(0.99, 1), (0.49, 300)]))
    assert price == 0.49 and size == 300


def test_no_bids_means_no_price():
    assert best_bid(_book(bids=[])) == (None, 0.0)


# ---------------------------------------------------------------------------
# the pair
# ---------------------------------------------------------------------------


def test_a_typical_one_cent_gap_is_found():
    # The live median: bids at 49/50 against a 100c redemption.
    client = _Client(_book(bids=[(0.49, 500)]), _book(bids=[(0.50, 500)]))
    pair = evaluate_maker_pair(client, _market())
    assert pair is not None
    assert pair.cost == pytest.approx(0.99)
    assert pair.edge == pytest.approx(0.01)
    assert pair.size_pairs == 500


def test_the_thinner_side_caps_the_size():
    client = _Client(_book(bids=[(0.49, 5000)]), _book(bids=[(0.50, 90)]))
    assert evaluate_maker_pair(client, _market()).size_pairs == 90


def test_bids_summing_to_a_dollar_are_not_an_opportunity():
    client = _Client(_book(bids=[(0.50, 500)]), _book(bids=[(0.50, 500)]))
    assert evaluate_maker_pair(client, _market()) is None


def test_bids_over_a_dollar_are_not_an_opportunity():
    client = _Client(_book(bids=[(0.55, 500)]), _book(bids=[(0.52, 500)]))
    assert evaluate_maker_pair(client, _market()) is None


def test_an_unreadable_book_is_skipped():
    client = _Client(_book(bids=[(0.49, 500)]), _book(bids=[(0.50, 500)]), raises=True)
    assert evaluate_maker_pair(client, _market()) is None


def test_a_closed_market_is_skipped():
    client = _Client(_book(bids=[(0.49, 500)]), _book(bids=[(0.50, 500)]))
    assert evaluate_maker_pair(client, _market(accepting=False)) is None


def test_a_one_sided_book_is_skipped():
    client = _Client(_book(bids=[(0.49, 500)]), _book(bids=[]))
    assert evaluate_maker_pair(client, _market()) is None


def test_profit_scales_with_size():
    client = _Client(_book(bids=[(0.49, 300)]), _book(bids=[(0.50, 300)]))
    assert evaluate_maker_pair(client, _market()).max_profit == pytest.approx(0.01 * 300)


# ---------------------------------------------------------------------------
# honesty
# ---------------------------------------------------------------------------


def test_every_pair_carries_the_fill_risk_warning():
    """This finder returns something on nearly every liquid market. Without
    the warning it reads as a list of free money."""
    client = _Client(_book(bids=[(0.49, 500)]), _book(bids=[(0.50, 500)]))
    pair = evaluate_maker_pair(client, _market())
    joined = " ".join(pair.warnings).lower()
    assert "may never fill" in joined
    assert "naked position" in joined


def test_uneven_depth_is_called_out():
    client = _Client(_book(bids=[(0.49, 5000)]), _book(bids=[(0.50, 60)]))
    pair = evaluate_maker_pair(client, _market())
    assert any("uneven" in w.lower() for w in pair.warnings)


# ---------------------------------------------------------------------------
# rewards
# ---------------------------------------------------------------------------


def test_liquidity_rewards_are_recorded():
    # A reward-paying market returns something while the bid rests, which is
    # exactly what this trade is short of.
    client = _Client(_book(bids=[(0.49, 500)]), _book(bids=[(0.50, 500)]))
    pair = evaluate_maker_pair(client, _market(reward=12.5))
    assert pair.daily_reward == 12.5 and pair.pays_rewards is True


def test_a_market_without_rewards_reports_zero():
    client = _Client(_book(bids=[(0.49, 500)]), _book(bids=[(0.50, 500)]))
    pair = evaluate_maker_pair(client, _market())
    assert pair.daily_reward == 0.0 and pair.pays_rewards is False


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def test_scanning_is_bounded_by_the_book_budget():
    class _Flat:
        def get_order_book(self, *, token_id):
            return _book(bids=[(0.50, 500)])

    _pairs, priced = find_maker_pairs(_Flat(), [_market(f"m{i}") for i in range(80)], max_books=6)
    assert priced == 6


def test_results_are_ranked_by_edge_then_rewards():
    class _Varying:
        def __init__(self):
            self.n = 0

        def get_order_book(self, *, token_id):
            self.n += 1
            # First market: 2c gap. Second: 1c gap.
            return _book(bids=[(0.49, 500)]) if self.n <= 2 else _book(bids=[(0.495, 500)])

    pairs, _priced = find_maker_pairs(_Varying(), [_market("wide"), _market("narrow")])
    assert [p.slug for p in pairs] == ["wide", "narrow"]


def test_rewards_break_a_tie_on_equal_edge():
    class _Same:
        def get_order_book(self, *, token_id):
            return _book(bids=[(0.495, 500)])

    pairs, _ = find_maker_pairs(_Same(), [_market("plain"), _market("paid", reward=20.0)])
    assert pairs[0].slug == "paid"


def test_to_dict_is_json_safe():
    import json

    pair = MakerPair(
        condition_id="0x", slug="s", question="q", url=None,
        yes_bid=0.49, no_bid=0.50, size_pairs=100,
    )
    json.dumps(pair.to_dict())
    assert pair.to_dict()["edge"] == pytest.approx(0.01)
