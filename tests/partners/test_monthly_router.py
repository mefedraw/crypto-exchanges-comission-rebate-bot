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

from bot.botui.keyboards import CabinetCB
from bot.db.models.identity import User
from bot.partners import rank_control
from bot.partners import router as partner_router
from tests.partners.test_monthly_ranks import JAN, PARENT, active_month, at, seed_monthly

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_monthly_dashboard_history_and_private_ownership(db_session, monkeypatch):
    db_session.sync_session.expire_on_commit = False
    accounts = await seed_monthly(db_session)
    await active_month(db_session, accounts[:1], 1)
    db_session.add(User(id=87999, role="user", status="active"))
    await db_session.flush()
    now = [JAN]
    monkeypatch.setattr(rank_control, "utc_now", lambda: now[0])
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

    async def click(data, *, actor=PARENT, group=False):
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
                    from_user=TelegramUser(id=actor, is_bot=False, first_name="User"),
                    message=Message(
                        message_id=10,
                        date=datetime.now(UTC),
                        text="Panel",
                        chat=Chat(
                            id=-100 if group else actor, type="group" if group else "private"
                        ),
                    ),
                ),
            ),
            session=db_session,
        )

    try:
        assert await click(CabinetCB(action="partner").pack(), group=True) is UNHANDLED
        await click(CabinetCB(action="partner").pack())
        panel = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "1/16" in panel.text and "Защита до" in panel.text and "Ранг: <b>7</b>" in panel.text
        now[0] = at(5)
        await click(CabinetCB(action="partner").pack())
        await click(module.PartnerCB(action="history").pack())
        panel = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "04.2026" in panel.text and "7 → 6" in panel.text
        await click(module.PartnerCB(action="history").pack(), actor=87999)
        own = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "пока нет" in own.text and "7 → 6" not in own.text
    finally:
        await bot.session.close()
        await dp.storage.close()
        importlib.reload(partner_router)
