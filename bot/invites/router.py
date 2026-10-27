"""Read-only personal invite link and invitation counters."""

from __future__ import annotations

import contextlib
from html import escape
from urllib.parse import urlencode

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import panel
from bot.botui.keyboards import CabinetCB, menu_back_keyboard
from bot.i18n import gettext as tr
from bot.invites import service

router = Router(name="user-invites")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class InviteCB(CallbackData, prefix="inv"):
    action: str
    token: str = "-"


def _link(username: str, code: str) -> str:
    return f"https://t.me/{username}?{urlencode({'start': service.PREFIX + code})}"


def _keyboard(link: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text=tr("Поделиться ссылкой"), url="https://t.me/share/url?" + urlencode({"url": link})
    )
    b.button(text=tr("Партнерская программа"), callback_data=CabinetCB(action="partner"))
    b.button(text=tr("В меню"), callback_data=CabinetCB(action="menu"))
    b.adjust(1)
    return b.as_markup()


@router.callback_query(CabinetCB.filter(F.action == "invite"))
async def open_invites(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    try:
        code = await service.current_code(session, callback.from_user.id)
        totals = await service.stats(session, callback.from_user.id)
        me = await bot.me()
        if not me.username:
            raise service.InviteError(
                tr("Не удалось получить ссылку бота. Пожалуйста, попробуйте позже.")
            )
        link = _link(me.username, code.code)
        text = tr(
            "<b>Пригласить друга</b>\n\nВаша ссылка:\n<code>{v0}</code>\n\nВаш код:"
            " <code>{v1}</code>\n\nПриглашено: <b>{v2}</b>\nПривязали UID: "
            "<b>{v3}</b>"
        ).format(v0=escape(link), v1=escape(code.code), v2=totals.invited, v3=totals.with_accounts)
        await session.commit()
        await state.clear()
        if isinstance(callback.message, Message):
            await panel.show(state, callback.message, text, _keyboard(link))
    except service.InviteError as exc:
        await callback.answer(tr(str(exc)), show_alert=True)
        return
    await callback.answer()


@router.callback_query(InviteCB.filter())
async def retired_custom_button(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if current and current.startswith("InviteFlow:"):
        await state.clear()
    await callback.answer(
        tr("Код выдается автоматически. Откройте «Пригласить друга»."), show_alert=True
    )
    if isinstance(callback.message, Message) and (
        current is None or current.startswith("InviteFlow:")
    ):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=menu_back_keyboard())
