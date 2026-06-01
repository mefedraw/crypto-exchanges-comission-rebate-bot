"""Rendering of bot messages (HTML for Telegram).

The result shows a single combined **USDT** figure (spot + futures merged) with
two decimals — other coins (MNT, USDC, …) are intentionally excluded — plus the
period the figure covers and the mandatory accrued-vs-settled disclaimer.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape

from bot.exchanges.base import CommissionResult
from bot.utils.dates import format_display
from bot.utils.money import format_2dp

DISCLAIMER = (
    "⚠️ Оценка <b>начисленной</b> комиссии по данным API. "
    "Для выплаты сверяйтесь с веб-кабинетом партнёра."
)


def usdt_total(result: CommissionResult) -> Decimal:
    """Sum of all USDT lines (across spot/futures); non-USDT coins ignored."""
    return sum(
        (line.amount for line in result.lines if line.asset.upper() == "USDT"),
        Decimal(0),
    )


def render_result(result: CommissionResult) -> str:
    total = usdt_total(result)
    period = f"{format_display(result.date_from.date())} — {format_display(result.date_to.date())}"

    lines: list[str] = [
        f"💰 <b>{escape(result.exchange)}</b>",
        f"👤 UID: <code>{escape(result.uid)}</code>",
        f"📆 Период: {period}",
        "",
        f"Комиссия: <b>{format_2dp(total)} USDT</b>",
    ]
    if total == 0:
        lines.append("<i>За период начислений в USDT не найдено.</i>")

    for note in result.notes:
        lines.append(f"\nℹ️ {escape(note)}")

    lines.append(f"\n{DISCLAIMER}")
    return "\n".join(lines)
