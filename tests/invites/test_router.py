from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from bot.botui import menu
from bot.botui.keyboards import CabinetCB, main_menu
from bot.db.models.identity import ExchangeAccount
from bot.db.models.invites import InviteAttribution, InviteCode
from bot.db.models.ledger import RebateAccrual
from bot.invites import router as invite_router
from bot.invites import service
from tests.invites.test_service import seed

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_short_link_and_no_custom_code_button():
    link = invite_router._link("test_bot", "a7k2m")
    assert link == "https://t.me/test_bot?start=r_a7k2m"
    buttons = [b for row in invite_router._keyboard(link).inline_keyboard for b in row]
    assert parse_qs(urlparse(buttons[0].url).query)["url"] == [link]
    assert not any((b.callback_data or "").startswith("inv:") for b in buttons)
    assert any(b.callback_data == "cab:invite" for row in main_menu().inline_keyboard for b in row)
    assert not invite_router.router.message.handlers


async def test_real_dispatcher_short_codes_and_removed_custom_handlers(db_session, monkeypatch):
    db_session.sync_session.expire_on_commit = False
    module = importlib.reload(invite_router)
    entry = importlib.reload(menu)
    dp = Dispatcher()
    dp.include_router(entry.router)
    dp.include_router(module.router)
    bot = Bot("123:abc")
    me = TelegramUser(id=123, is_bot=True, first_name="Bot", username="invite_test_bot")
    monkeypatch.setattr(bot, "me", AsyncMock(return_value=me))
    owner = await seed(db_session)
    code = owner.code
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

    def user(actor):
        return TelegramUser(id=actor, is_bot=False, first_name="User", username=f"name{actor}")

    async def feed_text(body, actor=82001, *, group=False):
        nonlocal number
        number += 1
        update = Update(
            update_id=number,
            message=Message(
                message_id=number + 100,
                date=datetime.now(UTC),
                from_user=user(actor),
                chat=Chat(id=-100 if group else actor, type="group" if group else "private"),
                text=body,
            ),
        )
        return await dp.feed_update(bot, update, session=db_session, admin_ids=frozenset())

    async def click(action, *, actor=82001):
        nonlocal number
        number += 1
        payload = (
            CabinetCB(action="invite").pack()
            if action == "open"
            else module.InviteCB(action=action).pack()
        )
        update = Update(
            update_id=number,
            callback_query=CallbackQuery(
                id=str(number),
                from_user=user(actor),
                chat_instance="test",
                data=payload,
                message=Message(
                    message_id=10,
                    date=datetime.now(UTC),
                    chat=Chat(id=actor, type="private"),
                    text="Panel",
                ),
            ),
        )
        await dp.feed_update(bot, update, session=db_session, admin_ids=frozenset())

    state = dp.fsm.get_context(bot=bot, chat_id=82001, user_id=82001)
    try:
        await click("open")
        initial = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert f"start=r_{code}" in initial.text and service._is_short_code(code)
        for action in ("custom", "edit", "save", "cancel"):
            await state.set_state("InviteFlow:code")
            await state.update_data(invite_code="new-custom")
            await click(action)
            assert await state.get_state() is None
            assert (await service.current_code(db_session, 82001)).code == code
        await state.set_state("InviteFlow:code")
        assert await feed_text("custom-name") is UNHANDLED
        assert (await service.current_code(db_session, 82001)).code == code
        await state.set_state("CredentialFlow:entering_credentials")
        methods.clear()
        await click("custom")
        assert await state.get_state() == "CredentialFlow:entering_credentials"
        assert not any(isinstance(m, EditMessageReplyMarkup) for m in methods)
        await state.clear()

        await feed_text(f"/start r_{code}", actor=82002)
        assert (await db_session.get(InviteAttribution, 82002)).referrer_id == 82001
        other = await seed(db_session, 82004)
        await feed_text(f"/start r_{other.code}", actor=82002)
        assert (await db_session.get(InviteAttribution, 82002)).referrer_id == 82001
        await feed_text(f"/start@invite_test_bot r_{code.upper()}", actor=82006)
        assert (await db_session.get(InviteAttribution, 82006)).referrer_id == 82001
        await feed_text(f"/start r_{code}", actor=82007, group=True)
        assert await db_session.get(InviteAttribution, 82007) is None
        await feed_text(f"/cabinet r_{code}", actor=82008)
        assert await db_session.get(InviteAttribution, 82008) is None
        for exchange in ("gate", "bybit"):
            db_session.add(
                ExchangeAccount(
                    user_id=82002,
                    exchange=exchange,
                    uid="acc-1",
                    status="active",
                    accrual_since=date(2026, 7, 10),
                )
            )
        await db_session.flush()
        await click("open")
        stats_text = [m for m in methods if isinstance(m, EditMessageText)][-1].text
        assert "Приглашено: <b>2</b>" in stats_text and "Привязали UID: <b>1</b>" in stats_text
        assert await db_session.scalar(select(func.count()).select_from(InviteCode)) == 2
        assert await db_session.scalar(select(func.count()).select_from(RebateAccrual)) == 0
    finally:
        await bot.session.close()
        await dp.storage.close()
        importlib.reload(menu)
