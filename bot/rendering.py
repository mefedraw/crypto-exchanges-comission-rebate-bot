"""Rendering of bot messages (HTML for Telegram).

The result shows accrued commission by asset. USDT lines are still merged and
displayed with two decimals; non-USDT assets are kept as separate currency lines.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape

from bot.exchanges.base import CommissionResult
from bot.utils.dates import format_display
from bot.utils.money import format_2dp, format_amount

DISCLAIMER = (
    "⚠️ Оценка <b>начисленной</b> комиссии по данным API. "
    "Для выплаты сверяйтесь с веб-кабинетом партнёра."
)


def usdt_total(result: CommissionResult) -> Decimal:
    """Sum of all USDT lines (across spot/futures)."""
    return sum(
        (line.amount for line in result.lines if line.asset.upper() == "USDT"),
        Decimal(0),
    )


def asset_totals(result: CommissionResult) -> dict[str, Decimal]:
    """Merge result lines by asset for display, preserving every currency."""
    totals: dict[str, Decimal] = {}
    for line in result.lines:
        asset = line.asset.upper()
        totals[asset] = totals.get(asset, Decimal(0)) + line.amount
    return dict(sorted(totals.items()))


def render_result(result: CommissionResult) -> str:
    totals = asset_totals(result)
    period = f"{format_display(result.date_from.date())} — {format_display(result.date_to.date())}"

    lines: list[str] = [
        f"💰 <b>{escape(result.exchange)}</b>",
        f"👤 UID: <code>{escape(result.uid)}</code>",
        f"📆 Период: {period}",
        "",
    ]
    if not totals:
        lines.extend(
            [
                "Комиссия: <b>0.00 USDT</b>",
                "<i>За период начислений не найдено.</i>",
            ]
        )
    elif set(totals) == {"USDT"}:
        lines.append(f"Комиссия: <b>{format_2dp(totals['USDT'])} USDT</b>")
    else:
        lines.append("Комиссия:")
        for asset, amount in totals.items():
            amount_text = format_2dp(amount) if asset == "USDT" else format_amount(amount)
            lines.append(f"• <b>{escape(amount_text)} {escape(asset)}</b>")

    for note in result.notes:
        lines.append(f"\nℹ️ {escape(note)}")

    lines.append(f"\n{DISCLAIMER}")
    return "\n".join(lines)
