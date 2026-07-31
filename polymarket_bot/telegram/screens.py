"""Screen text, rendered from structured data in the owner's language.

The bot used to hand `service.py`'s own `text` field straight to Telegram.
That had two problems the owner hit immediately:

  * **It was English.** Those strings are built inside `service.py`, so
    switching the bot to Hebrew changed the buttons and left every report in
    English. Translating them there would mean translating ~200 strings
    including money-safety warnings, where a mistranslation misleads.
  * **It was long.** `service.py` writes for a terminal, where a wide report
    with a disclaimer under it is the right shape. On a phone the same text is
    a wall.

So the screens are rendered here instead, from the structured fields those
functions already return. The English report stays exactly as it is for the
CLI; the chat gets a short, localized version of the same numbers, and the
numbers themselves are never re-derived - they are read from the response.

The market screen is the deliberate exception: it is the one place the owner
asked for depth, and it gets it.
"""

from __future__ import annotations

from polymarket_bot.telegram.i18n import t


def money(value: object) -> str:
    return f"${float(value):,.2f}" if isinstance(value, (int, float)) else "?"


def signed(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    return f"{'+' if value >= 0 else '-'}${abs(float(value)):,.2f}"


def cents(value: object, digits: int = 1) -> str:
    return f"{float(value) * 100:.{digits}f}¢" if isinstance(value, (int, float)) else "?"


def _pct(value: object, digits: int = 1) -> str:
    return f"{float(value):+.{digits}f}%" if isinstance(value, (int, float)) else "?"


# --------------------------------------------------------------------------
# short screens
# --------------------------------------------------------------------------


def redeem(response: dict, lang: str) -> str:
    """What a redeem actually did, in three lines instead of a transcript.

    The one nuance worth keeping: a losing position is redeemable and pays
    $0.00, so "redeemed 9 markets" with no money is not a bug and the screen
    has to say why.
    """
    if not response.get("ok"):
        return t("msg.error", lang, error=str(response.get("error")))

    count = int(response.get("count") or 0)
    failed = int(response.get("failed") or 0)
    total = float(response.get("total_usdc") or 0.0)

    if count == 0 and failed == 0:
        return t("redeem.none", lang)

    lines = [t("redeem.done", lang, count=count, amount=money(total))]
    if total < 0.01 and count:
        lines.append(t("redeem.worthless", lang))
    if failed:
        lines.append(t("redeem.failed", lang, count=failed))
    return "\n".join(lines)


def cancel(response: dict, lang: str) -> str:
    count = int(response.get("canceled_count") or 0)
    still = len(response.get("not_canceled") or {})
    if not response.get("ok") and not count and not still:
        return t("msg.error", lang, error=str(response.get("error")))
    lines = [t("cancel.done", lang, count=count)]
    if still:
        lines.append(t("cancel.stuck", lang, count=still))
    return "\n".join(lines)


def status(response: dict, lang: str) -> str:
    if not response.get("ok"):
        return t("msg.error", lang, error=str(response.get("error")))
    p = response.get("portfolio") or {}
    limits = response.get("limits") or {}
    lines = [
        t("status.title", lang),
        "",
        t("status.cash", lang, amount=money(p.get("cash_usdc"))),
        t("status.value", lang, amount=money(p.get("total_value"))),
        t("status.positions", lang, count=p.get("open_positions", 0)),
        t("status.pnl", lang, amount=signed(p.get("unrealized_pnl"))),
    ]
    redeemable = int(p.get("redeemable_count") or 0)
    if redeemable:
        lines.append(
            t("status.redeemable", lang, count=redeemable, amount=money(p.get("redeemable_value")))
        )
    orders = len(response.get("open_orders") or [])
    if orders:
        lines.append(t("status.orders", lang, count=orders))
    lines.append("")
    lines.append(
        t("status.limits", lang,
          order=money(limits.get("max_order_usdc")),
          market=money(limits.get("max_position_usdc")),
          daily=money(limits.get("daily_loss_limit_usdc")))
    )
    lines.append(
        t("status.rules", lang, count=response.get("active_rules", 0))
        + (" · " + t("status.dry_run", lang) if response.get("monitor_dry_run") else "")
    )
    return "\n".join(lines)


def analytics(response: dict, lang: str) -> str:
    """The trading record, as numbers rather than an essay."""
    if not response.get("ok"):
        return t("msg.error", lang, error=str(response.get("error")))
    s = response.get("stats") or {}
    total = int(s.get("total_trades") or 0)
    if not total:
        return t("record.none", lang)

    lines = [
        t("record.title", lang),
        "",
        t("record.trades", lang, count=total,
          wins=s.get("wins", 0), losses=s.get("losses", 0)),
        t("record.hit_rate", lang, pct=f"{float(s.get('win_rate') or 0):.0f}"),
        t("record.net", lang, amount=signed(s.get("net_pnl"))),
    ]
    best, worst = s.get("best_trade"), s.get("worst_trade")
    if isinstance(best, (int, float)):
        lines.append(t("record.best", lang, amount=signed(best)))
    if isinstance(worst, (int, float)):
        lines.append(t("record.worst", lang, amount=signed(worst)))

    insights = response.get("insights") or []
    if insights:
        lines.append("")
        lines.append(t("record.insights", lang))
        for item in insights[:4]:
            headline = str(item.get("headline") or item.get("title") or "").strip()
            if headline:
                lines.append(f"• {headline}")
    return "\n".join(lines)


def rules(response: dict, lang: str) -> str:
    if not response.get("ok"):
        return t("msg.error", lang, error=str(response.get("error")))
    stored = response.get("rules") or []
    if not stored:
        return t("rules.none", lang)
    lines = [t("rules.title", lang, count=len(stored)), ""]
    for rule in stored:
        target = rule.get("target_price")
        lines.append(
            t("rules.row", lang,
              kind=str(rule.get("kind") or ""),
              outcome=str(rule.get("outcome") or ""),
              title=str(rule.get("market_title") or "")[:38],
              target=cents(target) if target is not None else "-")
        )
    if response.get("monitor_dry_run"):
        lines.append("")
        lines.append(t("rules.dry_run", lang))
    return "\n".join(lines)


def monitor(response: dict, lang: str) -> str:
    if not response.get("ok"):
        return t("msg.error", lang, error=str(response.get("error")))
    report = response.get("report") or response
    triggered = report.get("triggered") or []
    checked = report.get("positions_checked", 0)
    evaluated = report.get("rules_evaluated", 0)
    lines = [t("monitor.title", lang), ""]
    lines.append(t("monitor.swept", lang, positions=checked, rules=evaluated))
    if not triggered:
        lines.append(t("monitor.quiet", lang))
    else:
        for entry in triggered:
            lines.append(
                t("monitor.fired", lang,
                  kind=str(entry.get("kind") or ""),
                  title=str(entry.get("market_title") or "")[:38])
            )
    if report.get("halted_reason"):
        lines.append("")
        lines.append(t("monitor.halted", lang))
    errors = report.get("errors") or []
    if errors:
        lines.append("")
        lines.append(t("monitor.errors", lang, count=len(errors)))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the deep screen
# --------------------------------------------------------------------------


def market_analysis(data: dict, lang: str) -> str:
    """The one screen that is meant to be long.

    Structured as: what it is, what the price means, what it costs to trade,
    what the book can take, where it has been, and what YOU could put in.
    """
    yes = data.get("yes") or {}
    no = data.get("no") or {}
    lines: list[str] = [data.get("question") or "", ""]

    # ---- what the price is saying ------------------------------------
    lines.append(t("an.prices", lang))
    for side in (yes, no):
        price = side.get("price")
        if price is None:
            lines.append(t("an.side_unpriced", lang, label=side.get("label") or "?"))
            continue
        lines.append(
            t("an.side", lang,
              label=str(side.get("label") or "?"),
              price=cents(price),
              implied=f"{float(price) * 100:.0f}")
        )
    lines.append(t("an.implied_note", lang))
    lines.append("")

    # ---- the chart ----------------------------------------------------
    chart = data.get("chart") or ""
    if chart:
        history = data.get("history") or []
        change = data.get("history_change")
        lines.append(t("an.chart_title", lang))
        lines.append(chart)
        if history:
            lines.append(
                t("an.chart_range", lang,
                  low=cents(min(history)), high=cents(max(history)),
                  change=cents(change) if change is not None else "?")
            )
        lines.append("")

    # ---- what trading it costs ----------------------------------------
    lines.append(t("an.cost", lang))
    round_trip = yes.get("round_trip")
    if round_trip is not None:
        lines.append(t("an.round_trip", lang, amount=cents(round_trip, 2)))
        lines.append(t("an.break_even", lang, price=cents(yes.get("price"))))
    spread = data.get("spread")
    if spread is not None:
        lines.append(t("an.spread", lang, amount=cents(spread, 2)))
    pair = data.get("pair_cost")
    if pair is not None:
        lines.append(t("an.pair", lang, amount=cents(pair)))
    lines.append("")

    # ---- how much the book can take -----------------------------------
    depth = yes.get("depth_shares")
    if depth:
        lines.append(t("an.depth", lang, shares=f"{float(depth):,.0f}"))
    volume = data.get("volume_24h")
    if volume:
        lines.append(t("an.volume", lang, amount=money(volume)))
    days = data.get("days_left")
    if days is not None:
        lines.append(
            t("an.ends_today", lang) if days <= 0 else t("an.days", lang, days=days)
        )
    reward = data.get("daily_reward")
    if reward:
        lines.append(t("an.rewards", lang, amount=f"{float(reward):g}"))
    lines.append("")

    # ---- what you could actually put in --------------------------------
    affordable = data.get("affordable_usdc")
    if affordable is not None:
        lines.append(t("an.your_size", lang, amount=money(affordable)))
        price = yes.get("price")
        if price:
            lines.append(
                t("an.your_shares", lang, shares=f"{float(affordable) / float(price):,.0f}")
            )

    for note in data.get("notes") or []:
        lines.append(f"⚠️ {note}")

    lines.append("")
    lines.append(t("an.disclaimer", lang))
    return "\n".join(lines)
