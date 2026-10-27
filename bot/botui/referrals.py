# bot/botui/referrals.py
"""Referral catalog screens: the "🔗 Рефералки" menu and per-exchange detail.

Read-only navigation over the static ``referrals_catalog``: the menu lists every
exchange as a button; tapping one edits the live panel to show that exchange's
share link plus its ref code in a <code> block (tap-to-copy on mobile). No DB,
no FSM state beyond the shared panel — pure presentation.
"""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.botui import panel
from bot.botui.keyboards import (
    CabinetCB,
    ReferralCB,
    referral_detail_keyboard,
    referral_menu_keyboard,
)
from bot.botui.referrals_catalog import get_referral
from bot.i18n import gettext as tr
from bot.logging import get_logger

router = Router(name="botui-referrals")
log = get_logger(__name__)

_MENU_TEXT = (
    "<b>🔗 Реферальные ссылки</b>\n\n"
    "Выберите биржу — покажу ссылку и реф-код.\n"
    "Реф-код копируется одним тапом (удобно при регистрации через приложение)."
)


def referral_detail_text(slug: str, *, custom_emoji: bool = True) -> str | None:
    """Detail panel for one exchange: link + tap-to-copy ref code, or None if unknown."""
    entry = get_referral(slug)
    if entry is None:
        return None
    icon = (
        f'<tg-emoji emoji-id="{entry.icon_custom_emoji_id}">{escape(entry.icon_emoji)}</tg-emoji>'
        if custom_emoji
        else escape(entry.emoji)
    )
    name = f"{icon} {escape(entry.name)}"
    if entry.pending:
        name = f"<s>{name}</s>"
    note = f" — {escape(tr(entry.note))}" if entry.note else ""
    status = tr("\n<b>В ожидании</b>") if entry.pending else ""
    return tr(
        "<b>{v0}</b>{v1}{v2}\n\n🔗 Ссылка:\n{v3}\n\n📋 Реф-код (нажмите, чтобы "
        "скопировать):\n<code>{v4}</code>"
    ).format(v0=name, v1=note, v2=status, v3=escape(entry.url), v4=escape(entry.ref_code))


async def _show(callback: CallbackQuery, state: FSMContext, slug: str | None = None) -> None:
    if not isinstance(callback.message, Message):
        return
    for custom in (True, False):
        text = tr(_MENU_TEXT) if slug is None else referral_detail_text(slug, custom_emoji=custom)
        if text is None:
            return
        keyboard = (
            referral_menu_keyboard(custom_emoji=custom)
            if slug is None
            else referral_detail_keyboard()
        )
        try:
            await panel.show(state, callback.message, text, keyboard)
        except TelegramBadRequest as exc:
            if not custom or "emoji" not in exc.message.lower():
                raise
            log.warning("referral_custom_emoji_rejected")
        else:
            return


@router.callback_query(CabinetCB.filter(F.action == "ref"))
async def open_referrals(callback: CallbackQuery, state: FSMContext) -> None:
    await _show(callback, state)
    await callback.answer()


@router.callback_query(ReferralCB.filter())
async def show_referral(
    callback: CallbackQuery, callback_data: ReferralCB, state: FSMContext
) -> None:
    if get_referral(callback_data.slug) is None:
        await callback.answer(tr("Биржа не найдена"), show_alert=True)
        return
    await _show(callback, state, callback_data.slug)
    await callback.answer()
