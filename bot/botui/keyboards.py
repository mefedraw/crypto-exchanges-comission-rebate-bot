# bot/botui/keyboards.py
"""Inline keyboards and typed callback data for the self-service cabinet."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.botui.referrals_catalog import REFERRALS
from bot.config import EXCHANGE_SPECS
from bot.db.models.identity import ExchangeAccount
from bot.i18n import gettext as tr


class CabinetCB(CallbackData, prefix="cab"):
    action: str  # "wallet" | "uids" | "stats" | "withdraw" | "ref" | "menu"


class ReferralCB(CallbackData, prefix="ref"):
    slug: str  # ReferralEntry.slug — which referral detail to show


class UidExchangeCB(CallbackData, prefix="uidex"):
    code: str


class UidRemoveCB(CallbackData, prefix="uidrm"):
    account_id: int


class UidRemoveConfirmCB(CallbackData, prefix="uidrmok"):
    account_id: int


class WithdrawConfirmCB(CallbackData, prefix="wdok"):
    token: str


class WithdrawStartCB(CallbackData, prefix="wd"):
    asset: str  # "USDT" | "MNT"


def main_menu(*, show_mnt: bool = False) -> InlineKeyboardMarkup:
    """``show_mnt`` gates the "Вывести MNT" button — the caller decides based on
    ``balance_summary(...).mnt_native > 0`` (an empty MNT pool has nothing to show)."""
    b = InlineKeyboardBuilder()
    b.button(text=tr("💳 Кошелёк"), callback_data=CabinetCB(action="wallet"))
    b.button(text=tr("🆔 UID биржи"), callback_data=CabinetCB(action="uids"))
    b.button(text=tr("📊 Статистика"), callback_data=CabinetCB(action="stats"))
    b.button(text=tr("🔗 Рефералки"), callback_data=CabinetCB(action="ref"))
    b.button(text=tr("💸 Вывести USDT"), callback_data=WithdrawStartCB(asset="USDT"))
    if show_mnt:
        b.button(text=tr("💸 Вывести MNT"), callback_data=WithdrawStartCB(asset="MNT"))
    b.button(
        text=tr("Техническая поддержка"),
        callback_data="case:menu:0:-",
        icon_custom_emoji_id="5404494366603522339",
    )
    b.button(
        text=tr("Пригласить друга"),
        callback_data=CabinetCB(action="invite"),
        icon_custom_emoji_id="5404830391959858439",
    )
    b.button(text=tr("Язык / Language"), callback_data=CabinetCB(action="language"))
    b.adjust(2, 2, 2 if show_mnt else 1, 1, 1, 1)
    return b.as_markup()


def referral_menu_keyboard(*, custom_emoji: bool = True) -> InlineKeyboardMarkup:
    """Grid of every referral exchange (2 per row) + a "⬅️ В меню" back button.

    Tapping an exchange fires ``ReferralCB(slug=...)`` → the detail screen with
    that exchange's link and tap-to-copy ref code."""
    b = InlineKeyboardBuilder()
    for entry in REFERRALS:
        status = tr(" (в ожидании)") if entry.pending else ""
        b.button(
            text=f"{entry.name}{status}" if custom_emoji else f"{entry.emoji} {entry.name}{status}",
            callback_data=ReferralCB(slug=entry.slug),
            icon_custom_emoji_id=entry.icon_custom_emoji_id if custom_emoji else None,
        )
    b.adjust(2)
    back = InlineKeyboardBuilder()
    back.button(text=tr("⬅️ В меню"), callback_data=CabinetCB(action="menu"))
    b.attach(back)
    return b.as_markup()


def referral_detail_keyboard() -> InlineKeyboardMarkup:
    """Back to the referral list, or all the way to the main menu."""
    b = InlineKeyboardBuilder()
    b.button(text=tr("⬅️ Рефералки"), callback_data=CabinetCB(action="ref"))
    b.button(text=tr("⬅️ В меню"), callback_data=CabinetCB(action="menu"))
    b.adjust(2)
    return b.as_markup()


def menu_back_keyboard() -> InlineKeyboardMarkup:
    """Single "⬅️ В меню" button — every leaf screen carries it so navigation stays
    in-place (the user never has to re-/start to get back)."""
    b = InlineKeyboardBuilder()
    b.button(text=tr("⬅️ В меню"), callback_data=CabinetCB(action="menu"))
    return b.as_markup()


def _exchange_choice_builder() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for spec in EXCHANGE_SPECS:
        b.button(text=spec.label, callback_data=UidExchangeCB(code=spec.code))
    b.adjust(2)
    return b


def uid_exchange_keyboard() -> InlineKeyboardMarkup:
    return _exchange_choice_builder().as_markup()


def uid_list_keyboard(accounts: Sequence[ExchangeAccount]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for account in accounts:
        b.button(
            text=f"❌ {account.exchange}: {account.uid}",
            callback_data=UidRemoveCB(account_id=account.id),
        )
    b.adjust(1)
    b.attach(_exchange_choice_builder())
    menu = InlineKeyboardBuilder()
    menu.button(text=tr("⬅️ В меню"), callback_data=CabinetCB(action="menu"))
    b.attach(menu)
    return b.as_markup()


def uid_remove_confirm_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Confirm step before unbinding a UID (reversible — the owner can re-add it)."""
    b = InlineKeyboardBuilder()
    b.button(text=tr("✅ Да, отвязать"), callback_data=UidRemoveConfirmCB(account_id=account_id))
    b.button(text=tr("⬅️ Отмена"), callback_data=CabinetCB(action="uids"))
    b.adjust(1)
    return b.as_markup()


def uid_back_keyboard() -> InlineKeyboardMarkup:
    """Shown while waiting for a UID — lets the user return to the list without typing."""
    b = InlineKeyboardBuilder()
    b.button(text=tr("⬅️ Назад к списку"), callback_data=CabinetCB(action="uids"))
    return b.as_markup()


def withdraw_confirm_keyboard(token: object) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("✅ Подтвердить вывод"), callback_data=WithdrawConfirmCB(token=str(token)))
    b.button(text=tr("⬅️ В меню"), callback_data=CabinetCB(action="menu"))
    b.adjust(1)
    return b.as_markup()
