# bot/botui/accounts.py
"""Add/list/remove exchange-account UIDs: first-come collision, no trade rights implied.

UX: a single "live panel" message. Navigation edits it in place; the UID the user types
is deleted after processing, so the chat stays clean. After adding, the panel returns to
the UID list so the next exchange can be tapped without re-opening the menu.
"""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.alerts import Alerter
from bot.botui import panel, referral, service
from bot.botui import repository as repo
from bot.botui.keyboards import (
    CabinetCB,
    UidExchangeCB,
    UidRemoveCB,
    UidRemoveConfirmCB,
    uid_back_keyboard,
    uid_list_keyboard,
    uid_remove_confirm_keyboard,
)
from bot.botui.states import UidFlow
from bot.config import get_settings
from bot.db.models.identity import ExchangeAccount
from bot.exchanges.base import ReferralCheck
from bot.i18n import gettext as tr

router = Router(name="botui-accounts")


def _format_list(accounts: list[ExchangeAccount], header: str = "") -> str:
    if not accounts:
        body = tr("У вас пока нет привязанных UID.\nВыберите биржу, чтобы добавить:")
    else:
        lines = [f"• {a.exchange}: {a.uid}" for a in accounts]
        body = tr("Ваши UID:\n") + "\n".join(lines) + tr("\n\nВыберите биржу, чтобы добавить ещё:")
    return header + body


def _add_uid_header(result: service.AddUidResult, typed_uid: str) -> str:
    """Panel banner for an add-UID attempt, reflecting the referral verdict."""
    safe = escape(typed_uid.strip())
    if result.outcome == "not_referral":
        return tr(
            "🚫 UID <code>{v0}</code> не найден среди ваших рефералов на этой "
            "бирже.\nПроверьте, что это реферал именно вашего аккаунта и на "
            "правильной бирже.\n\n"
        ).format(v0=safe)
    if result.outcome == "collision":
        return tr("⚠️ UID <code>{v0}</code> уже занят другим пользователем.\n\n").format(v0=safe)
    account = result.account
    added = (
        tr("✅ Добавлен {v0}: {v1}").format(v0=account.exchange, v1=account.uid)
        if account
        else tr("✅ Добавлен {v0}").format(v0=safe)
    )
    if result.status is ReferralCheck.CONFIRMED:
        return tr("{v0} — реферал подтверждён.\n\n").format(v0=added)
    return tr(
        "{v0}\n⚠️ Владение не подтверждено (биржа не даёт это проверить) — "
        "в расчёт всё равно попадёт только реальный рефбек.\n\n"
    ).format(v0=added)


@router.callback_query(CabinetCB.filter(F.action == "uids"))
async def list_uids(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    accounts = await repo.list_accounts(session, callback.from_user.id)
    await state.set_state(UidFlow.choosing_exchange)
    if isinstance(callback.message, Message):
        await panel.show(
            state, callback.message, _format_list(accounts), uid_list_keyboard(accounts)
        )
    await callback.answer()


@router.callback_query(UidFlow.choosing_exchange, UidExchangeCB.filter())
async def choose_exchange(
    callback: CallbackQuery, callback_data: UidExchangeCB, state: FSMContext
) -> None:
    await state.update_data(exchange=callback_data.code)
    await state.set_state(UidFlow.waiting_uid)
    if isinstance(callback.message, Message):
        await panel.show(
            state,
            callback.message,
            tr("Пришлите UID для <b>{v0}</b>:").format(v0=escape(callback_data.code)),
            uid_back_keyboard(),
        )
    await callback.answer()


@router.message(UidFlow.waiting_uid, F.text)
async def receive_uid(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, alerter: Alerter
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    uid_text = message.text or ""
    # Keep the chat clean: drop the UID the user just typed.
    await panel.delete_message(message)

    data = await state.get_data()
    exchange = data.get("exchange")
    if exchange is None:
        await state.clear()
        await message.answer(tr("Сессия истекла. Откройте кабинет заново: /start"))
        return

    settings = get_settings()

    async def _verifier(ex: str, u: str) -> ReferralCheck:
        return await referral.verify_referral(settings, ex, u)

    header = ""
    try:
        result = await service.add_uid(
            session,
            message.from_user.id,
            exchange,
            uid_text,
            settings.rebate_epoch,
            verifier=_verifier,
        )
        header = _add_uid_header(result, uid_text)
        # A first-come collision is a dispute the OWNER resolves (reassign). Alert them
        # with the exact reassign command so they can decide who gets the UID.
        if result.outcome == "collision" and result.held_by is not None:
            clean_uid = uid_text.strip()
            await alerter.send(
                "Спор по UID",
                f"Пользователь {message.from_user.id} пытается привязать "
                f"{exchange}:{clean_uid}, уже закреплён за {result.held_by}. "
                f"Переназначить (админка → 🔀 Переназначить UID): "
                f"{exchange} {clean_uid} <user_id>",
            )
    except service.UidError as exc:
        header = f"⚠️ {escape(str(exc))}\n\n"

    accounts = await repo.list_accounts(session, message.from_user.id)
    text, kb = _format_list(accounts, header), uid_list_keyboard(accounts)
    await state.set_state(UidFlow.choosing_exchange)
    await panel.edit_or_answer(bot, state, message, text, kb)


@router.callback_query(UidRemoveCB.filter())
async def confirm_remove_prompt(
    callback: CallbackQuery, callback_data: UidRemoveCB, state: FSMContext, session: AsyncSession
) -> None:
    """Step 1: confirm before unbinding — the unbind is reversible (owner can re-add)."""
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    accounts = await repo.list_accounts(session, callback.from_user.id)
    acc = next((a for a in accounts if a.id == callback_data.account_id), None)
    label = f"{acc.exchange}: {acc.uid}" if acc is not None else "UID"
    text = tr(
        "Отвязать <b>{v0}</b>?\n\nНачисления по нему остановятся. Уже "
        "накопленное останется доступным к выводу, и вы сможете вернуть "
        "этот UID позже — просто привязав его заново."
    ).format(v0=escape(label))
    if isinstance(callback.message, Message):
        await panel.show(
            state, callback.message, text, uid_remove_confirm_keyboard(callback_data.account_id)
        )
    await callback.answer()


@router.callback_query(UidRemoveConfirmCB.filter())
async def do_remove_uid(
    callback: CallbackQuery,
    callback_data: UidRemoveConfirmCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Step 2: actually archive (reversible)."""
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    removed = await repo.archive_account(session, callback.from_user.id, callback_data.account_id)
    accounts = await repo.list_accounts(session, callback.from_user.id)
    header = (
        tr("🗑 UID отвязан. Вернуть можно, привязав его заново.\n\n")
        if removed
        else tr("UID не найден.\n\n")
    )
    await state.set_state(UidFlow.choosing_exchange)
    if isinstance(callback.message, Message):
        await panel.show(
            state, callback.message, _format_list(accounts, header), uid_list_keyboard(accounts)
        )
    await callback.answer()
