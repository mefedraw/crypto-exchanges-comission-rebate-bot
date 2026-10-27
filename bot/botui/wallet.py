# bot/botui/wallet.py
"""Add/replace the BEP20 payout wallet: prompt -> validate -> encrypt -> persist.

Single-panel UX: the prompt edits the cabinet message in place, the address the user
types is deleted, and the result is written back into the same panel."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import panel, service
from bot.botui import repository as repo
from bot.botui.keyboards import CabinetCB, main_menu, menu_back_keyboard
from bot.botui.states import WalletFlow
from bot.i18n import gettext as tr
from bot.security.secrets import SecretCipher

router = Router(name="botui-wallet")


@router.callback_query(CabinetCB.filter(F.action == "wallet"))
async def prompt_wallet(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.set_state(WalletFlow.waiting_address)
    if isinstance(callback.message, Message) and callback.from_user is not None:
        wallet = await repo.get_active_wallet(session, callback.from_user.id)
        current = (
            tr("Текущий кошелёк: <code>{v0}</code>\n\n").format(v0=wallet.address_masked)
            if wallet is not None
            else tr("Кошелёк ещё не задан.\n\n")
        )
        await panel.show(
            state,
            callback.message,
            tr("{v0}Пришлите ваш <b>BEP20 (BSC)</b> адрес (0x…). Он заменит текущий.").format(
                v0=current
            ),
            menu_back_keyboard(),
        )
    await callback.answer()


@router.message(WalletFlow.waiting_address, F.text)
async def receive_wallet(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    cipher: SecretCipher | None,
    bot: Bot,
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    # Keep the chat clean: drop the address the user just typed.
    await panel.delete_message(message)

    if cipher is None:
        await panel.edit_or_answer(
            bot,
            state,
            message,
            tr("⚠️ Кошелёк временно недоступен: сервер не настроен (нет MASTER_KEY)."),
            menu_back_keyboard(),
        )
        return
    try:
        masked = await service.set_wallet(session, cipher, message.from_user.id, message.text or "")
    except service.AddressError as exc:
        # Stay in waiting_address so the next message is another attempt.
        await panel.edit_or_answer(
            bot,
            state,
            message,
            tr("⚠️ {v0}\nПришлите корректный BEP20-адрес (0x…) или вернитесь в меню.").format(
                v0=exc
            ),
            menu_back_keyboard(),
        )
        return
    await state.set_state(None)  # leave the flow; keep the tracked panel for navigation
    await panel.edit_or_answer(
        bot,
        state,
        message,
        tr("✅ Кошелёк сохранён: <code>{v0}</code>").format(v0=masked),
        main_menu(),
    )
