"""Rendering of bot messages (HTML for Telegram).

Centralizes user-facing text so the mandatory accrued-vs-settled disclaimer is
never accidentally dropped from a result.
"""

from __future__ import annotations

from datetime import date
from html import escape

from bot.exchanges.base import CommissionResult
from bot.utils.dates import DATE_FORMAT
from bot.utils.money import format_amount

DISCLAIMER = (
    "⚠️ Оценка <b>начисленной</b> комиссии по данным API. "
    "Для выплаты сверьтесь с веб-кабинетом партнёра "
    "(для Bybit и при расхождениях — обязательно)."
)


def render_confirm(exchange_label: str, uid: str, date_from: date, date_to: date) -> str:
    return (
        "Проверьте запрос:\n\n"
        f"<b>Биржа:</b> {escape(exchange_label)}\n"
        f"<b>UID:</b> <code>{escape(uid)}</code>\n"
        f"<b>Период:</b> {date_from:{DATE_FORMAT}} — {date_to:{DATE_FORMAT}}"
    )


def render_result(result: CommissionResult) -> str:
    lines: list[str] = [
        f"<b>Биржа:</b> {escape(result.exchange)}",
        f"<b>UID:</b> <code>{escape(result.uid)}</code>",
        f"<b>Период:</b> {result.date_from:{DATE_FORMAT}} — {result.date_to:{DATE_FORMAT}}",
        "",
    ]

    if result.is_empty:
        lines.append("По этому UID за период начислений не найдено.")
    else:
        lines.append("<b>Начислено (по данным API):</b>")
        for line in result.lines:
            source = f" ({escape(line.source)})" if line.source else ""
            lines.append(f"  • {format_amount(line.amount)} {escape(line.asset)}{source}")
        if result.total_usdt is not None:
            lines.append(f"  Σ ≈ {format_amount(result.total_usdt)} USDT")

    lines.append("")
    lines.append(f"Записей обработано: {result.raw_records_count}")

    for note in result.notes:
        lines.append(f"ℹ️ {escape(note)}")

    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
