"""/start and /help handlers."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards import start_keyboard

router = Router(name="start")

_WELCOME = (
    "👋 Бот расчёта реферальной комиссии.\n\n"
    "Выберите биржу, введите UID реферала и период — бот посчитает начисленную "
    "комиссию по данным API биржи.\n\n"
    "Команды: /start — начать заново, /help — помощь, /cancel — отменить."
)

_HELP = (
    "Поток: <b>биржа → UID → период → результат</b>.\n\n"
    "• UID — латинские буквы и цифры.\n"
    "• Даты — в формате ГГГГ-ММ-ДД, либо пресеты (вчера / 7 / 30 дней).\n"
    "• Суммы — <b>начисленные</b> по API; источник истины для выплат — кабинет партнёра.\n\n"
    "/cancel — сбросить текущий расчёт."
)


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_WELCOME, reply_markup=start_keyboard())


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(_HELP)
