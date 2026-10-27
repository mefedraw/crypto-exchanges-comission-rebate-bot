# bot/botui/withdraw.py
"""Withdraw flow: eligibility checks -> single-use confirm token -> reserved payout.

Ends at payouts(status='reserved') + accruals available->reserved. The on-chain
send is Phase 5 — this flow only reserves the money and stops.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import panel, service
from bot.botui.keyboards import (
    WithdrawConfirmCB,
    WithdrawStartCB,
    main_menu,
    menu_back_keyboard,
    withdraw_confirm_keyboard,
)
from bot.botui.states import WithdrawFlow
from bot.config import get_settings
from bot.i18n import gettext as tr
from bot.payout.expiry import PAYOUT_TZ
from bot.payout.ranks import rank_label
from bot.utils.money import format_2dp

router = Router(name="botui-withdraw")

# CLAUDE.md: every bot response carries the "accrued, not guaranteed payable" note.
_DISCLAIMER = "⚠️ Суммы — начисленные биржами данные, не гарантия к выплате."

_BLOCK_MESSAGES = {
    "paused": "⏸ Выплаты временно приостановлены администратором.",
    "blocked": "🚫 Ваш аккаунт заблокирован, обратитесь к администратору.",
    "no_wallet": "⚠️ Сначала привяжите кошелёк через «💳 Кошелёк».",
    "rank0": f"⚠️ Недостаточно начислений для вывода ({rank_label(0)}).",
    "pending": "⚠️ У вас уже есть заявка на вывод в обработке.",
    "below_min": f"⚠️ Сумма к выводу ниже минимальной ({format_2dp(service.MIN_PAYOUT_USDT)} USDT).",
    "mnt_disabled": "⚠️ Вывод в MNT временно недоступен.",
}


def _last_payout_line(last_at: datetime | None) -> str:
    """Last successful withdrawal, or "ещё не было" when there was none. The caller
    passes the last payout of the asset being withdrawn (cooldowns are per-asset)."""
    if last_at is None:
        return tr("Последняя выплата: ещё не было")
    local = last_at.astimezone(PAYOUT_TZ)
    return tr("Последняя выплата: {v0:%Y-%m-%d %H:%M} (UTC+8)").format(v0=local)


def _blocked_text(blocked: service.WithdrawBlocked, last_at: datetime | None) -> str:
    if blocked.reason == "cooldown":
        head = tr("⚠️ Вывод будет доступен после {v0}.").format(v0=blocked.detail)
    else:
        if blocked.reason == "rank0":
            head = tr("⚠️ Недостаточно начислений для вывода ({v0}).").format(v0=rank_label(0))
        elif blocked.reason == "below_min":
            head = tr("⚠️ Сумма к выводу ниже минимальной ({v0} USDT).").format(
                v0=format_2dp(service.MIN_PAYOUT_USDT)
            )
        else:
            head = tr(_BLOCK_MESSAGES.get(blocked.reason, "⚠️ Вывод сейчас недоступен."))
    return f"{head}\n{_last_payout_line(last_at)}"


def _preview_text(result: service.WithdrawPreview, last_at: datetime | None) -> str:
    last_line = _last_payout_line(last_at)
    if result.asset == "MNT":
        return tr(
            "Заявка на вывод <b>{v0} MNT</b> (≈{v1} USDT) на {v2}.\n{v3}\n"
            "Подтвердите отправку заявки.\n⏳ Выплата MNT — после подтверждения "
            "администратором.\n\n{v4}"
        ).format(
            v0=format_2dp(result.net_native),
            v1=format_2dp(result.net),
            v2=result.wallet_masked,
            v3=last_line,
            v4=tr(_DISCLAIMER),
        )
    return tr(
        "Заявка на вывод <b>{v0} USDT</b> на {v1}.\n{v2}\nПодтвердите " "отправку заявки.\n\n{v3}"
    ).format(v0=format_2dp(result.net), v1=result.wallet_masked, v2=last_line, v3=tr(_DISCLAIMER))


@router.callback_query(WithdrawStartCB.filter())
async def start_withdraw(
    callback: CallbackQuery,
    callback_data: WithdrawStartCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    user_id = callback.from_user.id
    now = datetime.now(UTC)
    today = now.astimezone(PAYOUT_TZ).date()
    result = await service.prepare_withdrawal(
        session, user_id, now, today, asset=callback_data.asset
    )
    # Per-asset cooldown: show the last payout OF THIS asset, so a cooldown block reads
    # consistently ("доступно после X" alongside "Последняя выплата: <this asset's last>").
    last_at = await service.last_withdrawal_at(session, user_id, asset=callback_data.asset)
    if isinstance(result, service.WithdrawBlocked):
        if isinstance(callback.message, Message):
            await panel.show(
                state, callback.message, _blocked_text(result, last_at), menu_back_keyboard()
            )
        await callback.answer()
        return
    token = await service.issue_withdraw_token(session, user_id, result, now)
    await state.set_state(WithdrawFlow.confirming)
    if isinstance(callback.message, Message):
        await panel.show(
            state,
            callback.message,
            _preview_text(result, last_at),
            withdraw_confirm_keyboard(token),
        )
    await callback.answer()


@router.callback_query(WithdrawFlow.confirming, WithdrawConfirmCB.filter())
async def confirm_withdraw(
    callback: CallbackQuery,
    callback_data: WithdrawConfirmCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    now = datetime.now(UTC)
    result = await service.confirm_withdrawal(
        session, callback.from_user.id, uuid.UUID(callback_data.token), now
    )
    await state.set_state(None)  # leave the flow; keep the tracked panel for navigation
    if isinstance(callback.message, Message):
        if isinstance(result, service.WithdrawBlocked):
            # Paused/blocked flipped on during the 5-min token window (TOCTOU re-check).
            last_at = await service.last_withdrawal_at(session, callback.from_user.id)
            text = _blocked_text(result, last_at)
        elif result is not None and result.asset == "MNT":
            text = tr(
                "✅ Заявка на вывод {v0} MNT создана.\n⏳ Выплата MNT — после "
                "подтверждения администратором.\n\n{v1}"
            ).format(v0=format_2dp(result.net_native), v1=tr(_DISCLAIMER))
        elif result is not None:
            text = tr(
                "✅ Заявка на вывод {v0} USDT создана, отправим в ближайшее время.\n"
                "Пришлём уведомление, когда средства дойдут до получателя.\n\n{v1}"
            ).format(v0=format_2dp(result.net_usdt), v1=tr(_DISCLAIMER))
        else:
            text = tr(
                "⚠️ Срок подтверждения истёк или заявка уже обрабатывается — " "начните вывод заново"
            )
        today = now.astimezone(PAYOUT_TZ).date()
        summary = await service.balance_summary(session, callback.from_user.id, now, today)
        # FIX M3: same gate as menu._show_mnt_button — a residual MNT balance must
        # not offer the button once MANTLE_RPC_URL is unset.
        show_mnt = get_settings().mnt_enabled and summary.mnt_native > 0
        await panel.show(state, callback.message, text, main_menu(show_mnt=show_mnt))
    await callback.answer()
