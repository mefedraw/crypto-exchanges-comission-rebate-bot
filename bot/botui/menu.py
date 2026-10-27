# bot/botui/menu.py
"""Cabinet entry point: /start, /cabinet, /cancel, and the "back to menu" callback."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import panel, service
from bot.botui import repository as repo
from bot.botui.keyboards import CabinetCB, main_menu
from bot.config import get_settings
from bot.db.models.identity import User
from bot.i18n import current_locale
from bot.i18n import gettext as tr
from bot.invites import service as invite_service
from bot.payout.expiry import PAYOUT_TZ

router = Router(name="botui-menu")

_MENU_TEXT = (
    "<b>Личный кабинет</b>\n\n"
    "Управляйте кошельком для выплат, привязанными UID бирж, смотрите статистику "
    "и запрашивайте вывод накопленной комиссии.\n\n"
    "⚠️ Суммы — начисленные биржами данные, не гарантия к выплате."
)


async def _show_mnt_button(session: AsyncSession, user_id: int) -> bool:
    """Gate the "Вывести MNT" button on the feature flag AND a non-empty MNT pool.

    FIX M3: if ``MANTLE_RPC_URL`` is removed after MNT was live, a residual MNT
    balance must not keep offering a withdraw that would only fail closed later
    (see ``service.prepare_withdrawal``) — so require ``settings.mnt_enabled``
    first, before even looking at the pool."""
    if not get_settings().mnt_enabled:
        return False
    now = datetime.now(UTC)
    today = now.astimezone(PAYOUT_TZ).date()
    summary = await service.balance_summary(session, user_id, now, today)
    return summary.mnt_native > 0


@router.message(CommandStart())
@router.message(Command("cabinet"))
async def open_cabinet(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_ids: frozenset[int],
    command: CommandObject | None = None,
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    await state.clear()
    private = getattr(getattr(message, "chat", None), "type", "private") == "private"
    payload = command.args if command and command.command == "start" and private else None
    registration = await invite_service.register_start(
        session,
        user_id=message.from_user.id,
        username=message.from_user.username,
        payload=payload,
        now=datetime.now(UTC),
    )
    if registration.created:
        await session.execute(
            update(User).where(User.id == message.from_user.id).values(locale=current_locale())
        )
    await repo.ensure_role(session, message.from_user.id, admin_ids)
    show_mnt = await _show_mnt_button(session, message.from_user.id)
    # Fresh entry point: post the menu and remember it as the live panel that every
    # later action will edit in place.
    notes = {
        "invited": tr("Приглашение учтено.\n\n"),
        "existing": (
            tr(
                "Вы уже зарегистрированы. Приглашение учитывается только при "
                "первом запуске бота.\n\n"
            )
        ),
        "self": tr("Это ваша ссылка. Самоприглашение не учитывается.\n\n"),
        "invalid": tr("Пригласительная ссылка недействительна. Личный кабинет доступен.\n\n"),
    }
    # Make first-registration attribution durable before acknowledging /start.
    await session.commit()
    sent = await message.answer(
        notes.get(registration.outcome, "") + tr(_MENU_TEXT),
        reply_markup=main_menu(show_mnt=show_mnt),
    )
    await panel.remember(state, sent)


@router.callback_query(CabinetCB.filter(F.action == "menu"))
async def back_to_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    # Leaving any flow: fully reset it and edit the panel back to the menu in place
    # (panel.show re-remembers the same message after clear()).
    await state.clear()
    if isinstance(callback.message, Message):
        show_mnt = await _show_mnt_button(session, callback.from_user.id)
        await panel.show(state, callback.message, tr(_MENU_TEXT), main_menu(show_mnt=show_mnt))
    await callback.answer()


@router.message(Command("cancel"))
async def cancel_to_menu(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    await state.clear()
    await repo.upsert_user(session, message.from_user.id, message.from_user.username)
    show_mnt = await _show_mnt_button(session, message.from_user.id)
    sent = await message.answer(
        tr("Отменено.\n\n") + tr(_MENU_TEXT), reply_markup=main_menu(show_mnt=show_mnt)
    )
    await panel.remember(state, sent)
