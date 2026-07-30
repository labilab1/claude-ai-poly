"""Service-layer and CLI tests for the arbitrage scan.

The report is the product here, so what is pinned is what it SAYS: an
opportunity whose edge does not survive fees must be labelled as not a trade,
and the leg-risk warning must be present. A report that reads like a
recommendation is the failure mode that costs money.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot import service
from polymarket_bot.arbitrage import ArbOpportunity
from polymarket_bot.config import Settings
from polymarket_bot.scripts import arbitrage as arb_script


@pytest.fixture(autouse=True)
def _isolated_settings():
    with mock.patch.object(
        service, "load_settings",
        return_value=Settings(private_key="0x0", wallet="0xw", data_dir=Path(".")),
    ):
        yield


def _opportunity(**overrides):
    base = dict(
        condition_id="0xc", slug="s", question="Will the thing happen?",
        url="https://polymarket.com/event/e/s",
        yes_price=0.45, no_price=0.50, size_pairs=200.0, fee_rate=0.02,
    )
    base.update(overrides)
    return ArbOpportunity(**base)


def _run_service(found, priced=30, **kwargs):
    with mock.patch.object(service, "list_tradable_markets", return_value=[]), \
         mock.patch.object(
             service.arbitrage_mod, "find_arbitrage", return_value=(found, priced)
         ):
        return service.arbitrage(client=SimpleNamespace(), **kwargs)


# ---------------------------------------------------------------------------
# the empty case is the normal case
# ---------------------------------------------------------------------------


def test_finding_nothing_is_a_success_not_an_error():
    response = _run_service([], priced=40)
    assert response["ok"] is True
    assert response["count"] == 0
    assert response["priced"] == 40


def test_the_empty_branch_still_reports_every_key():
    # A caller should not have to tell "zero tradable" apart from "the key is
    # missing because nothing was found".
    response = _run_service([], priced=40)
    assert response["tradable_count"] == 0


def test_the_empty_report_explains_why_nothing_was_found():
    # Otherwise "no arbitrage" reads like the scan is broken.
    response = _run_service([], priced=40)
    assert "market makers" in response["text"]


def test_the_report_says_how_many_were_priced_not_how_many_were_pulled():
    response = _run_service([], priced=12)
    assert "12" in response["text"]


# ---------------------------------------------------------------------------
# reporting a find honestly
# ---------------------------------------------------------------------------


def test_a_find_reports_the_legs_the_cost_and_the_edge():
    response = _run_service([_opportunity()])
    text = response["text"]
    assert "45.0c" in text and "50.0c" in text  # the two legs
    assert "95.0c" in text                       # what they cost together
    assert "100c" in text                        # what they redeem for


def test_an_edge_that_survives_fees_is_marked_positive():
    response = _run_service([_opportunity(yes_price=0.40, no_price=0.45)])
    assert "still positive" in response["text"]
    assert response["tradable_count"] == 1


def test_an_edge_eaten_by_fees_is_called_out_as_not_a_trade():
    """The honest case. A 2c edge at a 5% taker fee loses money, and a report
    that lists it without saying so is an invitation to lose it."""
    response = _run_service([_opportunity(yes_price=0.49, no_price=0.49, fee_rate=0.05)])
    assert "FEES EAT THIS" in response["text"]
    assert response["tradable_count"] == 0


def test_the_leg_risk_warning_is_always_present():
    response = _run_service([_opportunity()])
    text = response["text"].lower()
    assert "both legs must fill" in text
    assert "naked position" in text


def test_the_report_says_the_bot_will_not_execute_it():
    response = _run_service([_opportunity()])
    assert "will not execute" in response["text"].lower()


def test_warnings_from_the_engine_reach_the_report():
    response = _run_service([_opportunity(warnings=["Legs are uneven (1000 YES vs 20 NO)"])])
    assert "Legs are uneven" in response["text"]


def test_rows_are_json_safe():
    response = _run_service([_opportunity()])
    json.dumps(response["opportunities"])


def test_the_disclaimer_rides_along():
    response = _run_service([_opportunity()])
    assert response["disclaimer"]


# ---------------------------------------------------------------------------
# the scan is ordered by volume and bounded
# ---------------------------------------------------------------------------


def test_markets_are_pulled_volume_first():
    # An edge on a market with no size behind it is not money.
    with mock.patch.object(service, "list_tradable_markets", return_value=[]) as listing, \
         mock.patch.object(service.arbitrage_mod, "find_arbitrage", return_value=([], 0)):
        service.arbitrage(client=SimpleNamespace())
    assert listing.call_args.kwargs["order"] == "volume24hr"
    assert listing.call_args.kwargs["ascending"] is False


def test_the_book_budget_is_passed_through():
    with mock.patch.object(service, "list_tradable_markets", return_value=[]), \
         mock.patch.object(
             service.arbitrage_mod, "find_arbitrage", return_value=([], 0)
         ) as finder:
        service.arbitrage(max_books=7, client=SimpleNamespace())
    assert finder.call_args.kwargs["max_books"] == 7


def test_a_custom_fee_rate_is_used():
    with mock.patch.object(service, "list_tradable_markets", return_value=[]), \
         mock.patch.object(
             service.arbitrage_mod, "find_arbitrage", return_value=([], 0)
         ) as finder:
        service.arbitrage(fee_rate=0.05, client=SimpleNamespace())
    assert finder.call_args.kwargs["fee_rate"] == 0.05


def test_a_failure_inside_the_scan_becomes_a_failed_response():
    with mock.patch.object(service, "list_tradable_markets", side_effect=RuntimeError("gamma down")):
        response = service.arbitrage(client=SimpleNamespace())
    assert response["ok"] is False
    assert "gamma down" in response["error"]


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def test_cli_prints_the_report_and_exits_zero(capsys):
    with mock.patch.object(
        arb_script.service, "arbitrage", return_value={"ok": True, "text": "REPORT", "count": 0}
    ):
        code = arb_script.main([])
    assert code == 0 and "REPORT" in capsys.readouterr().out


def test_cli_exits_one_on_a_failed_scan(capsys):
    with mock.patch.object(
        arb_script.service, "arbitrage", return_value={"ok": False, "error": "boom"}
    ):
        code = arb_script.main([])
    assert code == 1 and "boom" in capsys.readouterr().out


def test_cli_json_mode_emits_the_raw_response(capsys):
    payload = {"ok": True, "text": "t", "opportunities": [], "count": 0}
    with mock.patch.object(arb_script.service, "arbitrage", return_value=payload):
        code = arb_script.main(["--json"])
    assert code == 0 and json.loads(capsys.readouterr().out) == payload


def test_cli_passes_its_flags_through():
    with mock.patch.object(
        arb_script.service, "arbitrage", return_value={"ok": True, "text": ""}
    ) as spy:
        arb_script.main(["--scan", "50", "--books", "9", "--fee", "0.05"])
    assert spy.call_args.kwargs == {"scan_limit": 50, "max_books": 9, "fee_rate": 0.05}


@pytest.mark.parametrize("argv", [["--fee", "1.5"], ["--fee", "-0.1"], ["--scan", "0"], ["--books", "0"]])
def test_cli_rejects_nonsense_arguments(argv):
    with pytest.raises(SystemExit):
        arb_script.main(argv)


def test_cli_maker_flag_scans_the_bid_side():
    with mock.patch.object(
        arb_script.service, "maker_pairs", return_value={"ok": True, "text": "MAKER"}
    ) as maker, mock.patch.object(
        arb_script.service, "arbitrage", side_effect=AssertionError("wrong scan")
    ):
        arb_script.main(["--maker"])
    assert maker.called


# ---------------------------------------------------------------------------
# maker report honesty
# ---------------------------------------------------------------------------


def _run_maker(pairs, priced=30):
    from polymarket_bot.arbitrage import MakerPair

    del MakerPair  # imported for clarity; pairs are supplied by the caller
    with mock.patch.object(service, "list_tradable_markets", return_value=[]), \
         mock.patch.object(
             service.arbitrage_mod, "find_maker_pairs", return_value=(pairs, priced)
         ):
        return service.maker_pairs(client=SimpleNamespace())


def _maker_pair(**overrides):
    from polymarket_bot.arbitrage import MakerPair

    base = dict(
        condition_id="0xc", slug="s", question="Will the thing happen?",
        url=None, yes_bid=0.49, no_bid=0.50, size_pairs=1000.0, daily_reward=0.0,
    )
    base.update(overrides)
    return MakerPair(**base)


def test_the_maker_report_calls_itself_market_making_not_free_money():
    response = _run_maker([_maker_pair()])
    assert "NOT FREE MONEY" in response["text"]


def test_the_maker_report_explains_adverse_selection():
    # The gap exists because of it; a report that omits it is selling a myth.
    response = _run_maker([_maker_pair()])
    assert "adverse selection" in response["text"].lower()


def test_the_maker_report_says_fills_are_not_guaranteed():
    response = _run_maker([_maker_pair()])
    assert "may never fill" in response["text"].lower()


def test_rewards_are_described_as_a_shared_pool_not_a_payout():
    """The rate is the market's whole daily pool split across every maker.
    Printing it as an individual payout would read as "$1000/day for you"
    on an account holding $21."""
    response = _run_maker([_maker_pair(daily_reward=1000.0)])
    text = response["text"]
    assert "ALL" in text and "your cut" in text.lower()


def test_a_market_without_rewards_says_nothing_about_them():
    response = _run_maker([_maker_pair(daily_reward=0.0)])
    assert "Rewards:" not in response["text"]


def test_the_maker_report_counts_reward_paying_markets():
    response = _run_maker([_maker_pair(daily_reward=5.0), _maker_pair(daily_reward=0.0)])
    assert response["rewarded_count"] == 1


def test_an_empty_maker_scan_is_still_ok():
    response = _run_maker([], priced=20)
    assert response["ok"] is True and response["count"] == 0
