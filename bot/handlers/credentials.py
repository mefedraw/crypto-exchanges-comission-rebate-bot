"""Admin-only Telegram flow for rotating affiliate API credentials."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.admin.filters import IsAdmin
from bot.admin.keyboards import AdminCB, admin_back_keyboard
from bot.config import SPEC_BY_CODE
from bot.security.credential_rotation import (
    CredentialInputError,
    CredentialRotationService,
    CredentialUpdaterError,
    CredentialValidationError,
    credential_prompt,
    parse_credential_message,
)
from bot.utils.validation import UidInputError, validate_uid

router = Router(name="credentials")

_K_EXCHANGE = "credential_exchange"
_K_UID = "credential_verification_uid"


class CredentialCB(CallbackData, prefix="cred"):
    action: str
    code: str | None = None


class CredentialFlow(StatesGroup):
    choosing_exchange = State()
    entering_uid = State()
    entering_credentials = State()
    applying = State()


def build_credentials_router(admin_ids: frozenset[int]) -> Router:
    """Apply the immutable env-admin gate and return the singleton router."""
    router.message.filter(IsAdmin(admin_ids), F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(
        IsAdmin(admin_ids),
        F.message.chat.type == ChatType.PRIVATE,
    )
    return router


def _service_unavailable_text() -> str:
    return (
        "Обновление API credentials отключено. Проверьте "
        "<code>CREDENTIAL_UPDATE_ENABLED</code>, <code>MASTER_KEY</code> и updater."
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=CredentialCB(action="cancel").pack(),
                )
            ]
        ]
    )


def _exchange_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code, spec in SPEC_BY_CODE.items():
        builder.button(
            text=spec.label,
            callback_data=CredentialCB(action="exchange", code=code),
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text="✖️ Отмена",
            callback_data=CredentialCB(action="cancel").pack(),
        )
    )
    return builder.as_markup()


async def _delete_sensitive_message(message: Message, state: FSMContext) -> bool:
    try:
        await message.delete()
    except Exception:
        await state.clear()
        await message.answer(
            "Не удалось удалить сообщение с credentials. Обновление отменено; "
            "удалите сообщение вручную."
        )
        return False
    return True


async def _show_exchange_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CredentialFlow.choosing_exchange)
    await message.answer(
        "Выберите биржу, для которой нужно заменить affiliate/read credentials:",
        reply_markup=_exchange_keyboard(),
    )


@router.message(Command("api_keys"))
async def cmd_api_keys(
    message: Message,
    state: FSMContext,
    credential_rotation: CredentialRotationService | None = None,
) -> None:
    if credential_rotation is None:
        await message.answer(_service_unavailable_text(), reply_markup=admin_back_keyboard())
        return
    await _show_exchange_menu(message, state)


@router.callback_query(AdminCB.filter(F.action == "credentials"))
async def on_admin_menu(
    callback: CallbackQuery,
    state: FSMContext,
    credential_rotation: CredentialRotationService | None = None,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if credential_rotation is None:
        await callback.message.answer(
            _service_unavailable_text(),
            reply_markup=admin_back_keyboard(),
        )
        return
    await _show_exchange_menu(callback.message, state)


@router.callback_query(
    CredentialFlow.choosing_exchange,
    CredentialCB.filter(F.action == "exchange"),
)
async def on_exchange(
    callback: CallbackQuery,
    callback_data: CredentialCB,
    state: FSMContext,
) -> None:
    spec = SPEC_BY_CODE.get(callback_data.code or "")
    if spec is None:
        await callback.answer("Неизвестная биржа.", show_alert=True)
        return
    await state.set_data({_K_EXCHANGE: spec.code})
    await state.set_state(CredentialFlow.entering_uid)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Введите действующий UID реферала на <b>{spec.label}</b>.\n"
            "Он нужен только для read-only проверки нового набора credentials."
        )


@router.message(CredentialFlow.entering_uid, F.text)
async def on_uid(message: Message, state: FSMContext) -> None:
    try:
        uid = validate_uid(message.text or "")
    except UidInputError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    spec = SPEC_BY_CODE.get(str(data.get(_K_EXCHANGE, "")))
    if spec is None:
        await state.clear()
        await message.answer("Сессия истекла. Запустите /api_keys заново.")
        return
    await state.update_data({_K_UID: uid})
    await state.set_state(CredentialFlow.entering_credentials)
    await message.answer(credential_prompt(spec), reply_markup=_cancel_keyboard())


@router.message(CredentialFlow.entering_credentials, F.text)
async def on_credentials(
    message: Message,
    state: FSMContext,
    credential_rotation: CredentialRotationService | None = None,
) -> None:
    if not await _delete_sensitive_message(message, state):
        return
    if credential_rotation is None:
        await state.clear()
        await message.answer(_service_unavailable_text())
        return

    data = await state.get_data()
    spec = SPEC_BY_CODE.get(str(data.get(_K_EXCHANGE, "")))
    uid = str(data.get(_K_UID, ""))
    if spec is None or not uid:
        await state.clear()
        await message.answer("Сессия истекла. Запустите /api_keys заново.")
        return

    try:
        bundle = parse_credential_message(spec, message.text or "")
    except CredentialInputError as exc:
        await state.set_state(CredentialFlow.entering_credentials)
        await message.answer(f"Неверный формат: {exc}\n\n{credential_prompt(spec)}")
        return

    await state.set_state(CredentialFlow.applying)
    status = await message.answer(
        f"Проверяю новые credentials через API {spec.label} и готовлю замену..."
    )
    actor = message.from_user
    if actor is None:
        await state.clear()
        await status.edit_text("Не удалось определить администратора. Обновление отменено.")
        return

    try:
        receipt = await credential_rotation.rotate(
            bundle,
            verification_uid=uid,
            actor_id=actor.id,
        )
    except CredentialValidationError as exc:
        await state.clear()
        await status.edit_text(str(exc), reply_markup=admin_back_keyboard())
        return
    except CredentialUpdaterError as exc:
        await state.clear()
        await status.edit_text(
            f"Credentials прошли проверку, но не сохранены: {exc}",
            reply_markup=admin_back_keyboard(),
        )
        return
    finally:
        del bundle

    await state.clear()
    await status.edit_text(
        f"Credentials {spec.label} с ключом <code>{receipt.masked_api_key}</code> "
        "проверены и приняты. Bot и accrual будут пересозданы автоматически.",
        reply_markup=admin_back_keyboard(),
    )


@router.message(CredentialFlow.entering_credentials)
async def on_non_text_credentials(message: Message, state: FSMContext) -> None:
    if not await _delete_sensitive_message(message, state):
        return
    await message.answer("Отправьте credentials обычным текстовым сообщением.")


@router.callback_query(CredentialCB.filter(F.action == "cancel"))
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()

    if current_state == CredentialFlow.applying.state:
        await callback.answer("Проверка уже выполняется.", show_alert=True)
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
        return

    cancelable_states = {
        CredentialFlow.choosing_exchange.state,
        CredentialFlow.entering_uid.state,
        CredentialFlow.entering_credentials.state,
    }
    if current_state not in cancelable_states:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.delete()
        return

    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.delete()
