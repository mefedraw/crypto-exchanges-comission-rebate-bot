"""Inline keyboards and their typed callback data.

Callback payloads use aiogram's CallbackData factory so parsing stays type-safe
and we never hand-split strings.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ExchangeCB(CallbackData, prefix="ex"):
    """Selecting an exchange from the menu."""

    code: str


class PresetCB(CallbackData, prefix="preset"):
    """Choosing a date preset instead of typing a range."""

    kind: str  # "yesterday" | "7d" | "30d" | "custom"


class NavCB(CallbackData, prefix="nav"):
    """Navigation / confirmation actions."""

    action: str  # "confirm" | "cancel" | "new" | "same_exchange" | "start"


_CANCEL_BUTTON = InlineKeyboardButton(text="✖️ Отмена", callback_data=NavCB(action="cancel").pack())


def _with_cancel(builder: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    builder.row(_CANCEL_BUTTON)
    return builder.as_markup()


def start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Рассчитать комиссию", callback_data=NavCB(action="start"))
    return builder.as_markup()


def exchanges_keyboard(exchanges: dict[str, str]) -> InlineKeyboardMarkup:
    """Menu of available exchanges, ``{code: label}`` in display order."""
    builder = InlineKeyboardBuilder()
    for code, label in exchanges.items():
        builder.button(text=label, callback_data=ExchangeCB(code=code))
    builder.adjust(2)  # two columns
    return _with_cancel(builder)


def date_presets_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Вчера", callback_data=PresetCB(kind="yesterday"))
    builder.button(text="7 дней", callback_data=PresetCB(kind="7d"))
    builder.button(text="30 дней", callback_data=PresetCB(kind="30d"))
    builder.button(text="Свой период", callback_data=PresetCB(kind="custom"))
    builder.adjust(3, 1)
    return _with_cancel(builder)


def confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=NavCB(action="confirm"))
    return _with_cancel(builder)


def result_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔁 Новый расчёт", callback_data=NavCB(action="new"))
    builder.button(text="↩️ Та же биржа, другой uid", callback_data=NavCB(action="same_exchange"))
    builder.adjust(1)
    return builder.as_markup()
