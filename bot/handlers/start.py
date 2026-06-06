"""/start and /help handlers."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards import start_keyboard

router = Router(name="start")

_WELCOME = (
    "👋 <b>Бот расчёта реферальной комиссии</b>\n\n"
    "Считаю начисленную комиссию по рефералу за период.\n"
    "Порядок: <b>биржа → UID → период → результат по валютам</b>.\n\n"
    "Нажмите кнопку ниже, чтобы начать 👇\n"
    "<i>Команды:</i> /start — заново · /help — помощь · /cancel — отмена"
)

_HELP = (
    "ℹ️ <b>Как пользоваться</b>\n\n"
    "1️⃣ Выберите <b>биржу</b>.\n"
    "2️⃣ Введите <b>UID</b> реферала (латиница и цифры).\n"
    "3️⃣ Выберите период:\n"
    "   🗓 <b>За месяц (авто)</b> — прошлый месяц (а ближе к концу месяца — текущий).\n"
    "   ✍️ <b>Указать период</b> — в формате <code>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</code>.\n\n"
    "📤 В ответе — суммы по валютам за выбранный период; USDT показывается с 2 знаками.\n\n"
    "⚠️ Это <b>начисленная</b> по API сумма; для выплат сверяйтесь с кабинетом партнёра.\n"
    "/cancel — сбросить текущий расчёт."
)


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_WELCOME, reply_markup=start_keyboard())


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(_HELP)
