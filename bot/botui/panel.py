# bot/botui/panel.py
"""Single "live panel" navigation for the cabinet.

The whole cabinet is ONE message that gets edited in place as the user navigates,
instead of posting a new message per action. Callback handlers edit the tapped
message directly (:func:`show`); text-input handlers (which can't edit the user's
typed message) delete it and edit the tracked panel by id (:func:`edit_by_id`).

The panel's (chat_id, message_id) is remembered in FSM state, which aiogram keys by
(chat, user) and shares across every router — so any handler can find the panel.
"""

from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message

_CHAT_KEY = "panel_chat"
_MSG_KEY = "panel_msg"


async def remember(state: FSMContext, msg: Message) -> None:
    """Record which message is the live panel, so later handlers can edit it."""
    await state.update_data({_CHAT_KEY: msg.chat.id, _MSG_KEY: msg.message_id})


async def show(state: FSMContext, source: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit ``source`` (the tapped message) in place; fall back to a fresh message.

    Used by callback handlers, where ``source`` is ``callback.message`` — i.e. the
    panel itself. A "message is not modified" edit is treated as success (the panel
    already shows this), never a duplicate post."""
    try:
        await source.edit_text(text, reply_markup=kb)
        await remember(state, source)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            await remember(state, source)
            return
        sent = await source.answer(text, reply_markup=kb)
        await remember(state, sent)


async def edit_by_id(bot: Bot, state: FSMContext, text: str, kb: InlineKeyboardMarkup) -> bool:
    """Edit the tracked panel by id (used after deleting the user's typed input).

    Returns False if there is no tracked panel or the edit fails, so the caller can
    fall back to sending a fresh message."""
    data = await state.get_data()
    chat, msg = data.get(_CHAT_KEY), data.get(_MSG_KEY)
    if not chat or not msg:
        return False
    try:
        await bot.edit_message_text(text, chat_id=chat, message_id=msg, reply_markup=kb)
        return True
    except TelegramBadRequest as exc:
        return "not modified" in str(exc).lower()


async def edit_or_answer(
    bot: Bot, state: FSMContext, fallback: Message, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Edit the tracked panel by id; if that's not possible, post a fresh panel via
    ``fallback`` and remember it. For text-input handlers, where the user's typed
    message was just deleted and there is no tapped message to edit."""
    if not await edit_by_id(bot, state, text, kb):
        sent = await fallback.answer(text, reply_markup=kb)
        await remember(state, sent)


async def delete_message(msg: Message) -> None:
    """Best-effort delete of the user's typed message to keep the chat clean."""
    with contextlib.suppress(TelegramBadRequest):
        await msg.delete()
