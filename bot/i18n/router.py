"""Private language settings; changing language never registers or attributes a user."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import menu, panel
from bot.botui.keyboards import CabinetCB, main_menu, menu_back_keyboard
from bot.db.models.identity import User
from bot.i18n import LANGUAGES, current_locale, remember_locale, use_locale
from bot.i18n import gettext as tr

router = Router(name="language-settings")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

LANGUAGE_FLAGS = {
    "ru": "🇷🇺",
    "en": "🇬🇧",
    "uk": "🇺🇦",
    "zh": "🇨🇳",
    "tr": "🇹🇷",
    "hi": "🇮🇳",
    "th": "🇹🇭",
    "vi": "🇻🇳",
}


class LanguageCB(CallbackData, prefix="lang"):
    code: str


def keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, name in LANGUAGES.items():
        b.button(
            text=("✓ " if code == current_locale() else "") + f"{LANGUAGE_FLAGS[code]} {name}",
            callback_data=LanguageCB(code=code),
        )
    b.adjust(2)
    b.row(*menu_back_keyboard().inline_keyboard[0])
    return b.as_markup()


@router.message(Command("language"))
async def language_command(message: Message, state: FSMContext) -> None:
    sent = await message.answer(tr("Выберите язык / Choose language"), reply_markup=keyboard())
    await panel.remember(state, sent)


@router.callback_query(CabinetCB.filter(F.action == "language"))
async def language_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, tr("Выберите язык / Choose language"), keyboard())
    await callback.answer()


@router.callback_query(LanguageCB.filter())
async def select_language(
    callback: CallbackQuery,
    callback_data: LanguageCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    code = callback_data.code
    if code not in LANGUAGES:
        await callback.answer(tr("Кнопка устарела."), show_alert=True)
        return
    user_id = callback.from_user.id
    changed = await session.scalar(
        update(User).where(User.id == user_id).values(locale=code).returning(User.id)
    )
    if changed is None:
        await callback.answer(tr("Сначала откройте личный кабинет через /start."), show_alert=True)
        return
    await session.commit()
    remember_locale(user_id, code)
    await state.clear()
    with use_locale(code):
        if isinstance(callback.message, Message):
            show_mnt = await menu._show_mnt_button(session, user_id)
            await panel.show(
                state, callback.message, tr(menu._MENU_TEXT), main_menu(show_mnt=show_mnt)
            )
        await callback.answer(tr("Язык сохранен."))
