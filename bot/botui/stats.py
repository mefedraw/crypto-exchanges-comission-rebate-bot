# bot/botui/stats.py
"""Balance/rank/fee/next-withdrawal/expiry view for the self-service cabinet."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import panel, service
from bot.botui import repository as repo
from bot.botui.keyboards import CabinetCB, menu_back_keyboard
from bot.db.models.identity import ExchangeAccount
from bot.i18n import gettext as tr
from bot.payout.expiry import PAYOUT_TZ
from bot.payout.rank_timing import month_start, next_month_start
from bot.payout.ranks import rank_label
from bot.utils.money import format_2dp

router = Router(name="botui-stats")

DISCLAIMER = "⚠️ Баланс рассчитан по начисленным биржами данным на момент запроса."


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return tr("доступно сейчас")
    local = dt.astimezone(PAYOUT_TZ)
    return f"{local:%Y-%m-%d %H:%M} (UTC+8)"


def _fmt_last_payout(dt: datetime | None) -> str:
    """Last successful withdrawal, or "ещё не было" when the user never withdrew.
    Distinct from `_fmt_dt` (whose "доступно сейчас" None-label makes no sense here)."""
    if dt is None:
        return tr("ещё не было")
    local = dt.astimezone(PAYOUT_TZ)
    return f"{local:%Y-%m-%d %H:%M} (UTC+8)"


def _fmt_account_month(assets: dict[str, tuple[Decimal, Decimal]]) -> str:
    """Per-UID monthly figure. A non-USDT rebate (e.g. MNT) is shown in its own coin
    with the USDT value in parentheses, e.g. ``438.95 MNT (194.13 USDT)``; a USDT
    rebate stays ``43.60 USDT``. Both pools shown when present."""
    parts: list[str] = []
    for asset in sorted(assets, key=lambda a: (a == "USDT", a)):  # non-USDT (MNT) first
        native, usdt = assets[asset]
        if native == 0 and usdt == 0:
            continue
        if asset == "USDT":
            parts.append(f"{format_2dp(usdt)} USDT")
        else:
            parts.append(f"{format_2dp(native)} {escape(asset)} ({format_2dp(usdt)} USDT)")
    return " + ".join(parts) if parts else "0.00 USDT"


def render_summary(
    summary: service.BalanceSummary,
    accounts: list[ExchangeAccount],
    account_month_assets: dict[int, dict[str, tuple[Decimal, Decimal]]],
) -> str:
    # Don't promise money the user can't take right now: only label it "доступно к
    # выводу" when the withdraw flow would actually accept it (rank ok, ≥ min, cooldown
    # passed, no pending payout). Otherwise show it as merely accrued. USDT and MNT are
    # SEPARATE pools with their own availability — show each on its own line, MNT in its
    # native coin with the USDT-equivalent in parentheses.
    balance_lines: list[str] = []
    usdt_label = tr("Доступно к выводу") if summary.withdrawable else tr("Накоплено")
    balance_lines.append(f"{usdt_label}: <b>{format_2dp(summary.available_net)} USDT</b>")
    if summary.mnt_native > 0:
        mnt_label = tr("Доступно к выводу") if summary.mnt_withdrawable else tr("Накоплено")
        balance_lines.append(
            f"{mnt_label} (MNT): <b>{format_2dp(summary.mnt_native)} MNT</b>"
            f" (~{format_2dp(summary.mnt_usdt)} USDT)"
        )
    if not summary.withdrawable and not summary.mnt_withdrawable:
        balance_lines.append(tr("⏳ Пока недоступно к выводу — причина при нажатии «💸 Вывести»"))

    # USDT and MNT cooldowns are independent. Show one combined "next payout" line for a
    # USDT-only user (the common case, unchanged), but split it per asset once the user
    # actually holds an MNT balance — otherwise a single line would hide that (say) MNT
    # is still on cooldown while USDT is free.
    if summary.mnt_native > 0:
        payout_lines = [
            tr("Ближайшая выплата USDT: {v0}").format(v0=_fmt_dt(summary.next_withdrawal_at)),
            tr("Ближайшая выплата MNT: {v0}").format(v0=_fmt_dt(summary.mnt_next_withdrawal_at)),
        ]
    else:
        payout_lines = [
            tr("Ближайшая доступная выплата: {v0}").format(v0=_fmt_dt(summary.next_withdrawal_at))
        ]

    lines = [
        tr("📊 <b>Статистика</b>"),
        "",
        *balance_lines,
        tr("Ранг: <b>{v0}</b>").format(v0=rank_label(summary.effective_rank)),
        tr("Последняя выплата: {v0}").format(v0=_fmt_last_payout(summary.last_withdrawal_at)),
        *payout_lines,
        tr("Ближайшее сгорание остатка: {v0}").format(v0=_fmt_dt(summary.nearest_expiry)),
        "",
    ]
    if accounts:
        lines.append(tr("Ваши UID:"))
        for a in accounts:
            assets = account_month_assets.get(a.id, {})
            lines.append(
                tr("• {v0}: <code>{v1}</code> — за месяц {v2}").format(
                    v0=escape(a.exchange), v1=escape(a.uid), v2=_fmt_account_month(assets)
                )
            )
    else:
        lines.append(tr("UID пока не добавлены — привяжите биржу через «🆔 UID биржи»."))
    lines.append("")
    lines.append(tr(DISCLAIMER))
    return "\n".join(lines)


@router.callback_query(CabinetCB.filter(F.action == "stats"))
async def show_stats(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    user_id = callback.from_user.id
    now = datetime.now(UTC)
    today = now.astimezone(PAYOUT_TZ).date()
    summary = await service.balance_summary(session, user_id, now, today)
    accounts = await repo.list_accounts(session, user_id)
    m_start = month_start(today)
    m_before = next_month_start(m_start)
    account_month_assets: dict[int, dict[str, tuple[Decimal, Decimal]]] = {
        a.id: await repo.account_month_by_asset(session, a.id, m_start, m_before) for a in accounts
    }
    if isinstance(callback.message, Message):
        await panel.show(
            state,
            callback.message,
            render_summary(summary, accounts, account_month_assets),
            menu_back_keyboard(),
        )
    await callback.answer()
