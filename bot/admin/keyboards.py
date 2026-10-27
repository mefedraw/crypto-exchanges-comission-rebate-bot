# bot/admin/keyboards.py
"""Inline keyboard and typed callback data for the admin panel."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class AdminCB(CallbackData, prefix="adm"):
    action: str  # "menu" | "killswitch" | "pending" | "rate" | "users" | "stats" |
    # "wallet" | "credentials" | "block_user" | "reassign_uid" | "block_uid"


def admin_back_keyboard() -> InlineKeyboardMarkup:
    """Single "⬅️ В меню админки" button — every admin leaf screen carries it so the
    whole panel navigates in place (mirrors the user cabinet's menu_back_keyboard)."""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ В меню админки", callback_data=AdminCB(action="menu"))
    return b.as_markup()


class PayoutActionCB(CallbackData, prefix="pay"):
    action: str  # "release" | "reject" | "reduce"
    payout_id: int


def payout_action_keyboard(payout_id: int, asset: str = "USDT") -> InlineKeyboardMarkup:
    """Per-payout action row for a `pending_admin` entry.

    The release button label is asset-specific: MNT payouts still need a manual
    on-chain send after approval, so the label reminds the admin of that step.
    """
    b = InlineKeyboardBuilder()
    release_text = "✅ Пополнил кошелёк MNT — отправить" if asset == "MNT" else "✅ Одобрить"
    b.button(text=release_text, callback_data=PayoutActionCB(action="release", payout_id=payout_id))
    b.button(
        text="❌ Отклонить", callback_data=PayoutActionCB(action="reject", payout_id=payout_id)
    )
    b.button(text="🎚 Занизить", callback_data=PayoutActionCB(action="reduce", payout_id=payout_id))
    b.button(text="⬅️ В меню админки", callback_data=AdminCB(action="menu"))
    b.adjust(3, 1)  # 3 actions on one row, the back button on its own
    return b.as_markup()


def user_controls_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu for the «🚫 Блок/UID-спор» admin action: block/unblock, reassign, block UID."""
    b = InlineKeyboardBuilder()
    b.button(text="🚫 Блок/разблок юзера", callback_data=AdminCB(action="block_user"))
    b.button(text="🔀 Переназначить UID", callback_data=AdminCB(action="reassign_uid"))
    b.button(text="⛔ Заблокировать UID", callback_data=AdminCB(action="block_uid"))
    b.button(text="⬅️ В меню админки", callback_data=AdminCB(action="menu"))
    b.adjust(1)
    return b.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⏸ Пауза выплат / ▶️ Возобновить", callback_data=AdminCB(action="killswitch"))
    b.button(text="💸 Крупные выплаты", callback_data=AdminCB(action="pending"))
    b.button(text="🎚 Ставка юзеру", callback_data=AdminCB(action="rate"))
    b.button(text="🚫 Блок/UID-спор", callback_data=AdminCB(action="users"))
    b.button(text="📊 Статистика", callback_data=AdminCB(action="stats"))
    b.button(text="💰 Баланс кошелька", callback_data=AdminCB(action="wallet"))
    b.button(text="🔑 Обновить API-ключи", callback_data=AdminCB(action="credentials"))
    b.button(text="Обращения", callback_data="case:admin:0:-")
    b.adjust(1)
    return b.as_markup()
