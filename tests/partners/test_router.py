from __future__ import annotations

import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from bot.botui.keyboards import CabinetCB
from bot.db.models.payouts import Payout
from bot.partners import router as partner_router
from tests.partners.test_service import PARENT
from tests.partners.test_withdrawal import ready

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_private_dashboard_rank_alternatives_and_explicit_withdrawal(db_session, monkeypatch):
    await ready(db_session)
    module = importlib.reload(partner_router)
    dp = Dispatcher()
    dp.include_router(module.router)
    bot = Bot("123:abc")
    methods = []
    number = 0

    async def request(_bot, method, **_kwargs):
        methods.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            return Message(
                message_id=10,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
                reply_markup=method.reply_markup,
            )
        return True

    monkeypatch.setattr(bot.session, "make_request", AsyncMock(side_effect=request))

    async def click(data, *, group=False):
        nonlocal number
        number += 1
        return await dp.feed_update(
            bot,
            Update(
                update_id=number,
                callback_query=CallbackQuery(
                    id=str(number),
                    chat_instance="test",
                    data=data,
                    from_user=TelegramUser(id=PARENT, is_bot=False, first_name="User"),
                    message=Message(
                        message_id=10,
                        date=datetime.now(UTC),
                        text="Panel",
                        chat=Chat(
                            id=-100 if group else PARENT, type="group" if group else "private"
                        ),
                    ),
                ),
            ),
            session=db_session,
        )

    try:
        assert await click(CabinetCB(action="partner").pack(), group=True) is UNHANDLED
        await click(CabinetCB(action="partner").pack())
        home = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "Партнерская программа" in home.text and "20.00 USDT" in home.text
        assert "84002" not in home.text
        await click(module.PartnerCB(action="ranks").pack())
        ranks = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert " или " in ranks.text and "10000+ USDT" in ranks.text and "0%" in ranks.text
        await click(module.PartnerCB(action="withdraw").pack())
        preview = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "20.00 USDT" in preview.text and "BEP20" in preview.text
        confirm = preview.reply_markup.inline_keyboard[0][0].callback_data
        assert len(confirm.encode()) <= 64
        assert await db_session.scalar(select(func.count()).select_from(Payout)) == 0
        await click(confirm)
        await click(confirm)
        assert await db_session.scalar(select(func.count()).select_from(Payout)) == 1
    finally:
        await bot.session.close()
        await dp.storage.close()
        importlib.reload(partner_router)
