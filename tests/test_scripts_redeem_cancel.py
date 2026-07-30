"""CLI-level tests for the redeem/cancel scripts. No network, no real orders.

Both scripts are pure exposure of service functions that already exist - what
is worth pinning is the exit-code convention (0 ok / 1 not), that `--json`
emits the raw response rather than a report, and that neither script invents
its own success text when the service call failed.
"""

from __future__ import annotations

import json
from unittest import mock

from polymarket_bot.scripts import cancel as cancel_script
from polymarket_bot.scripts import redeem as redeem_script


def _run(script, argv, response, capsys):
    """Run a script's main() with its service call stubbed, capturing stdout.

    Real stdout rather than a stubbed `emit`: `--json` renders through
    `_common.dump_json`, which holds its own reference to `_common.emit`, so
    patching the script's imported name would silently miss that path.
    """
    target = "redeem" if script is redeem_script else "cancel_orders"
    with mock.patch.object(script.service, target, return_value=response):
        code = script.main(argv)
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# redeem
# ---------------------------------------------------------------------------
def test_redeem_success_exits_zero_and_prints_service_text(capsys):
    code, out = _run(
        redeem_script,
        [],
        {"ok": True, "text": "Redeemed 2 of 2 settled market(s), paying $4.10.", "count": 2},
        capsys,
    )
    assert code == 0
    assert "Redeemed 2 of 2" in out


def test_redeem_failure_exits_one(capsys):
    code, out = _run(
        redeem_script, [], {"ok": False, "text": "Redeem failed", "error": "api down"}, capsys
    )
    assert code == 1
    assert "Redeem failed" in out


def test_redeem_json_mode_emits_the_raw_response(capsys):
    response = {"ok": True, "text": "Nothing to redeem.", "count": 0, "total_usdc": 0.0}
    code, out = _run(redeem_script, ["--json"], response, capsys)
    assert code == 0
    assert json.loads(out) == response


def test_redeem_nothing_to_claim_is_still_success(capsys):
    # An empty account is not an error - a scheduled run must not alarm on it.
    code, _out = _run(
        redeem_script,
        [],
        {"ok": True, "text": "Nothing to redeem - no settled positions.", "count": 0},
        capsys,
    )
    assert code == 0


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------
def test_cancel_success_exits_zero_and_prints_service_text(capsys):
    code, out = _run(
        cancel_script,
        [],
        {"ok": True, "text": "Cancelled 3 resting order(s).", "canceled_count": 3},
        capsys,
    )
    assert code == 0
    assert "Cancelled 3 resting order(s)." in out


def test_cancel_warns_that_resting_take_profits_die_with_the_orders(capsys):
    # The one non-obvious consequence: an offline-safe take-profit is a resting
    # limit order, and cancelling everything removes it.
    code, out = _run(
        cancel_script,
        [],
        {"ok": True, "text": "Cancelled 0 resting order(s).", "canceled_count": 0},
        capsys,
    )
    assert code == 0
    assert "take-profit" in out.lower()


def test_cancel_partial_failure_exits_one(capsys):
    # service.cancel_orders reports ok=False when an order is still resting.
    # The script must not round that up to success.
    code, out = _run(
        cancel_script,
        [],
        {
            "ok": False,
            "text": "Cancelled 1 resting order(s).\n  1 order(s) are STILL RESTING",
            "error": "1 of 2 order(s) could not be cancelled",
        },
        capsys,
    )
    assert code == 1
    assert "STILL RESTING" in out


def test_cancel_json_mode_emits_the_raw_response(capsys):
    response = {"ok": True, "text": "Cancelled 0 resting order(s).", "canceled": [], "canceled_count": 0}
    code, out = _run(cancel_script, ["--json"], response, capsys)
    assert code == 0
    assert json.loads(out) == response
