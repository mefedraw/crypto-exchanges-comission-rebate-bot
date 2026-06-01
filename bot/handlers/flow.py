"""The main conversation: exchange → uid → period → confirm → result.

State and data live in aiogram's FSM. Adapters are injected from the dispatcher
workflow data (``adapters``), so this module stays decoupled from exchange code.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.alerts import Alerter
from bot.config import SPEC_BY_CODE
from bot.exchanges.base import (
    ExchangeAdapter,
    ExchangeApiError,
    ExchangeAuthError,
    ExchangeRateLimitError,
    ExchangeUnavailableError,
)
from bot.keyboards import (
    ExchangeCB,
    NavCB,
    PresetCB,
    confirm_keyboard,
    date_presets_keyboard,
    exchanges_keyboard,
    result_keyboard,
    start_keyboard,
)
from bot.logging import get_logger
from bot.rendering import render_confirm, render_result
from bot.utils.dates import DateInputError, day_end, day_start, parse_date, validate_period
from bot.utils.validation import UidInputError, validate_uid

logger = get_logger(__name__)

router = Router(name="flow")

# FSM data keys.
_K_CODE = "exchange_code"
_K_LABEL = "exchange_label"
_K_UID = "uid"
_K_FROM = "date_from"
_K_TO = "date_to"


class Flow(StatesGroup):
    choosing_exchange = State()
    entering_uid = State()
    choosing_period = State()
    entering_date_from = State()
    entering_date_to = State()
    confirm = State()
    fetching = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exchange_menu(adapters: dict[str, ExchangeAdapter]) -> dict[str, str]:
    """``{code: label}`` for available exchanges, in canonical display order."""
    return {
        spec.code: spec.label
        for spec in SPEC_BY_CODE.values()
        if spec.code in adapters
    }


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _preset_range(kind: str) -> tuple[date, date]:
    today = _today()
    if kind == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if kind == "7d":
        return today - timedelta(days=6), today
    if kind == "30d":
        return today - timedelta(days=29), today
    raise ValueError(f"Unknown preset: {kind}")


async def _show_exchanges(message: Message, state: FSMContext, adapters: dict[str, ExchangeAdapter]) -> None:
    menu = _exchange_menu(adapters)
    if not menu:
        await message.edit_text("Биржи не настроены. Обратитесь к администратору.")
        await state.clear()
        return
    await state.set_state(Flow.choosing_exchange)
    await message.edit_text("Выберите биржу:", reply_markup=exchanges_keyboard(menu))


async def _go_confirm(message: Message, state: FSMContext, date_from: date, date_to: date) -> None:
    data = await state.get_data()
    await state.update_data({_K_FROM: date_from.isoformat(), _K_TO: date_to.isoformat()})
    await state.set_state(Flow.confirm)
    await message.answer(
        render_confirm(data[_K_LABEL], data[_K_UID], date_from, date_to),
        reply_markup=confirm_keyboard(),
    )


# ---------------------------------------------------------------------------
# Entry points: start / new / cancel
# ---------------------------------------------------------------------------


@router.callback_query(NavCB.filter(F.action == "start"))
@router.callback_query(NavCB.filter(F.action == "new"))
async def on_start_calc(callback: CallbackQuery, state: FSMContext, adapters: dict[str, ExchangeAdapter]) -> None:
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_exchanges(callback.message, state, adapters)


@router.callback_query(NavCB.filter(F.action == "cancel"))
async def on_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Отменено. /start — начать заново.")


@router.message(Command("cancel"))
async def on_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. /start — начать заново.", reply_markup=start_keyboard())


# ---------------------------------------------------------------------------
# Exchange chosen -> ask uid
# ---------------------------------------------------------------------------


@router.callback_query(Flow.choosing_exchange, ExchangeCB.filter())
async def on_exchange_chosen(
    callback: CallbackQuery,
    callback_data: ExchangeCB,
    state: FSMContext,
    adapters: dict[str, ExchangeAdapter],
) -> None:
    spec = SPEC_BY_CODE.get(callback_data.code)
    if spec is None or callback_data.code not in adapters:
        await callback.answer("Биржа недоступна.", show_alert=True)
        return
    await state.update_data({_K_CODE: spec.code, _K_LABEL: spec.label})
    await state.set_state(Flow.entering_uid)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"Биржа: <b>{spec.label}</b>\n\nВведите UID реферала:")


@router.callback_query(NavCB.filter(F.action == "same_exchange"))
async def on_same_exchange(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    label = data.get(_K_LABEL)
    code = data.get(_K_CODE)
    if not code or not label:
        await callback.answer("Сессия истекла, начните заново.", show_alert=True)
        return
    # Keep exchange, drop the rest.
    await state.set_state(Flow.entering_uid)
    await state.set_data({_K_CODE: code, _K_LABEL: label})
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(f"Биржа: <b>{label}</b>\n\nВведите UID реферала:")


# ---------------------------------------------------------------------------
# uid entered -> choose period
# ---------------------------------------------------------------------------


@router.message(Flow.entering_uid, F.text)
async def on_uid_entered(message: Message, state: FSMContext) -> None:
    try:
        uid = validate_uid(message.text or "")
    except UidInputError as exc:
        await message.answer(str(exc))
        return
    await state.update_data({_K_UID: uid})
    await state.set_state(Flow.choosing_period)
    await message.answer("Выберите период:", reply_markup=date_presets_keyboard())


# ---------------------------------------------------------------------------
# Period selection
# ---------------------------------------------------------------------------


@router.callback_query(Flow.choosing_period, PresetCB.filter())
async def on_preset_chosen(callback: CallbackQuery, callback_data: PresetCB, state: FSMContext) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    if callback_data.kind == "custom":
        await state.set_state(Flow.entering_date_from)
        await callback.message.edit_text("Введите дату начала (ГГГГ-ММ-ДД):")
        return

    try:
        date_from, date_to = _preset_range(callback_data.kind)
    except ValueError:
        await callback.message.edit_text("Неизвестный пресет. /start — заново.")
        await state.clear()
        return

    await callback.message.edit_text("Период выбран.")
    await _go_confirm(callback.message, state, date_from, date_to)


@router.message(Flow.entering_date_from, F.text)
async def on_date_from_entered(message: Message, state: FSMContext) -> None:
    try:
        date_from = parse_date(message.text or "")
    except DateInputError as exc:
        await message.answer(str(exc))
        return
    await state.update_data({_K_FROM: date_from.isoformat()})
    await state.set_state(Flow.entering_date_to)
    await message.answer("Введите дату конца (ГГГГ-ММ-ДД):")


@router.message(Flow.entering_date_to, F.text)
async def on_date_to_entered(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        date_from = date.fromisoformat(data[_K_FROM])
        date_to = parse_date(message.text or "")
        validate_period(date_from, date_to)
    except DateInputError as exc:
        await message.answer(str(exc))
        return
    await _go_confirm(message, state, date_from, date_to)


# ---------------------------------------------------------------------------
# Confirm -> fetch -> result
# ---------------------------------------------------------------------------


@router.callback_query(Flow.confirm, NavCB.filter(F.action == "confirm"))
async def on_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    adapters: dict[str, ExchangeAdapter],
    alerter: Alerter,
) -> None:
    data = await state.get_data()
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    adapter = adapters.get(data[_K_CODE])
    if adapter is None:
        await callback.message.edit_text("Биржа стала недоступна. /start — заново.")
        await state.clear()
        return

    uid: str = data[_K_UID]
    date_from = date.fromisoformat(data[_K_FROM])
    date_to = date.fromisoformat(data[_K_TO])
    user_id = callback.from_user.id

    await state.set_state(Flow.fetching)
    await callback.message.edit_text("⏳ Считаю…")

    log = logger.bind(
        user_id=user_id, exchange=data[_K_CODE], uid=uid,
        date_from=data[_K_FROM], date_to=data[_K_TO],
    )
    try:
        result = await adapter.get_commission(uid, day_start(date_from), day_end(date_to))
    except ExchangeAuthError as exc:
        await alerter.alert_exception(
            "Проблема с ключами API биржи",
            exc,
            context=f"exchange: {data[_K_CODE]}, uid: {uid}",
        )
        await _fail(callback.message, state, log, "auth", f"Проблема с доступом к API {data[_K_LABEL]}. Проверьте ключи.")
        return
    except ExchangeRateLimitError:
        await _fail(callback.message, state, log, "rate_limit", f"{data[_K_LABEL]} ограничивает частоту запросов. Попробуйте позже.")
        return
    except ExchangeUnavailableError:
        await _fail(callback.message, state, log, "unavailable", f"{data[_K_LABEL]} не ответила вовремя. Попробуйте позже.")
        return
    except ExchangeApiError as exc:
        await _fail(callback.message, state, log, "api_error", f"Ошибка при запросе к {data[_K_LABEL]}: {exc}")
        return

    log.info("request ok", records=result.raw_records_count, lines=len(result.lines))
    # Keep exchange for "same_exchange"; drop the rest.
    await state.set_state(None)
    await state.set_data({_K_CODE: data[_K_CODE], _K_LABEL: data[_K_LABEL]})
    await callback.message.answer(render_result(result), reply_markup=result_keyboard())


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


async def _fail(message: Message, state: FSMContext, log: Any, reason: str, text: str) -> None:
    log.warning("request failed", reason=reason)
    await state.set_state(None)
    await message.answer(text, reply_markup=result_keyboard())


# Catch stray text while we're waiting on a button press.
@router.message(StateFilter(Flow.choosing_exchange, Flow.choosing_period, Flow.confirm))
async def on_unexpected_text(message: Message) -> None:
    await message.answer("Пожалуйста, используйте кнопки выше или /cancel.")
