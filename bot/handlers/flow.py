"""The main conversation: exchange → uid → period → result.

State and data live in aiogram's FSM. Adapters are injected from the dispatcher
workflow data (``adapters``), so this module stays decoupled from exchange code.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
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
    date_presets_keyboard,
    exchanges_keyboard,
    result_keyboard,
    start_keyboard,
)
from bot.logging import get_logger
from bot.rendering import render_result
from bot.utils.dates import (
    DateInputError,
    day_end,
    day_start,
    format_display,
    parse_period,
    smart_month,
    validate_period,
)
from bot.utils.validation import UidInputError, validate_uid

# Reused prompts (with hints).
_UID_PROMPT = (
    "Введите <b>UID реферала</b> 👇\n"
    "<i>Только латинские буквы и цифры, например</i> <code>43305891</code>"
)
_PERIOD_PROMPT = "Выберите период расчёта:"
_CUSTOM_PROMPT = (
    "Введите период в формате <b>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</b> 👇\n"
    "<i>например</i> <code>01.05.2026-31.05.2026</code>"
)

logger = get_logger(__name__)

router = Router(name="flow")

# FSM data keys.
_K_CODE = "exchange_code"
_K_LABEL = "exchange_label"
_K_UID = "uid"


class Flow(StatesGroup):
    choosing_exchange = State()
    entering_uid = State()
    choosing_period = State()
    entering_period = State()
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


async def _show_exchanges(message: Message, state: FSMContext, adapters: dict[str, ExchangeAdapter]) -> None:
    menu = _exchange_menu(adapters)
    if not menu:
        await message.edit_text("Биржи не настроены. Обратитесь к администратору.")
        await state.clear()
        return
    await state.set_state(Flow.choosing_exchange)
    await message.edit_text("Выберите <b>биржу</b> 👇", reply_markup=exchanges_keyboard(menu))


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
        await callback.message.edit_text(f"Биржа: <b>{spec.label}</b>\n\n{_UID_PROMPT}")


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
        await callback.message.answer(f"Биржа: <b>{label}</b>\n\n{_UID_PROMPT}")


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
    await message.answer(_PERIOD_PROMPT, reply_markup=date_presets_keyboard())


# ---------------------------------------------------------------------------
# Period selection
# ---------------------------------------------------------------------------


@router.callback_query(Flow.choosing_period, PresetCB.filter())
async def on_preset_chosen(
    callback: CallbackQuery,
    callback_data: PresetCB,
    state: FSMContext,
    adapters: dict[str, ExchangeAdapter],
    alerter: Alerter,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    if callback_data.kind == "custom":
        await state.set_state(Flow.entering_period)
        await callback.message.edit_text(_CUSTOM_PROMPT)
        return

    if callback_data.kind != "smart_month":
        await callback.message.edit_text("Неизвестный период. /start — заново.")
        await state.clear()
        return

    date_from, date_to = smart_month(_today())
    await _fetch_and_render(
        callback.message, state, adapters, alerter, date_from, date_to, callback.from_user.id
    )


@router.message(Flow.entering_period, F.text)
async def on_period_entered(
    message: Message,
    state: FSMContext,
    adapters: dict[str, ExchangeAdapter],
    alerter: Alerter,
) -> None:
    try:
        date_from, date_to = parse_period(message.text or "")
        validate_period(date_from, date_to)
    except DateInputError as exc:
        await message.answer(str(exc))
        return
    await _fetch_and_render(
        message, state, adapters, alerter, date_from, date_to, message.from_user.id
    )


# ---------------------------------------------------------------------------
# Fetch -> result
# ---------------------------------------------------------------------------


async def _fetch_and_render(
    message: Message,
    state: FSMContext,
    adapters: dict[str, ExchangeAdapter],
    alerter: Alerter,
    date_from: date,
    date_to: date,
    user_id: int,
) -> None:
    data = await state.get_data()
    code, label, uid = data[_K_CODE], data[_K_LABEL], data[_K_UID]

    adapter = adapters.get(code)
    if adapter is None:
        await message.answer("Биржа стала недоступна. /start — заново.")
        await state.clear()
        return

    await state.set_state(Flow.fetching)
    period = f"{format_display(date_from)} — {format_display(date_to)}"
    status = await message.answer(f"⏳ Считаю комиссию…\n<i>{label}, {period}</i>")

    log = logger.bind(
        user_id=user_id, exchange=code, uid=uid,
        date_from=date_from.isoformat(), date_to=date_to.isoformat(),
    )
    try:
        result = await adapter.get_commission(uid, day_start(date_from), day_end(date_to))
    except ExchangeAuthError as exc:
        await alerter.alert_exception(
            "Проблема с ключами API биржи", exc, context=f"exchange: {code}, uid: {uid}"
        )
        await _fail(status, state, log, "auth", f"🔒 Проблема с доступом к API {label}. Проверьте ключи.")
        return
    except ExchangeRateLimitError:
        await _fail(status, state, log, "rate_limit", f"⏳ {label} ограничивает частоту запросов. Попробуйте позже.")
        return
    except ExchangeUnavailableError:
        await _fail(status, state, log, "unavailable", f"📡 {label} не ответила вовремя. Попробуйте позже.")
        return
    except ExchangeApiError as exc:
        await _fail(status, state, log, "api_error", f"⚠️ Ошибка при запросе к {label}: {exc}")
        return

    log.info("request ok", records=result.raw_records_count, lines=len(result.lines))
    # Keep exchange for "same_exchange"; drop the rest.
    await state.set_state(None)
    await state.set_data({_K_CODE: code, _K_LABEL: label})
    await status.edit_text(render_result(result), reply_markup=result_keyboard())


async def _fail(message: Message, state: FSMContext, log: Any, reason: str, text: str) -> None:
    log.warning("request failed", reason=reason)
    await state.set_state(None)
    await message.edit_text(text, reply_markup=result_keyboard())


# Catch stray text while we're waiting on a button press.
@router.message(StateFilter(Flow.choosing_exchange, Flow.choosing_period))
async def on_unexpected_text(message: Message) -> None:
    await message.answer("Пожалуйста, используйте кнопки выше или /cancel.")
