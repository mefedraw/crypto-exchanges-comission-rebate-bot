from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from bot.botui.keyboards import main_menu
from bot.db.models.support import SupportCase, SupportMessage
from bot.support import keyboards as kb
from bot.support import router as support_router
from bot.support import service, statuses
from bot.support.keyboards import CaseCB
from tests.support.test_service import ADMINS, seed

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_button_always_visible_and_callback_payloads_fit():
    for show_mnt in (False, True):
        buttons = [b for row in main_menu(show_mnt=show_mnt).inline_keyboard for b in row]
        support = [b for b in buttons if b.text == "Техническая поддержка"]
        assert len(support) == 1
        assert CaseCB.unpack(support[0].callback_data).action == "menu"
        assert any(row == support for row in main_menu(show_mnt=show_mnt).inline_keyboard)
        assert not any(b.text in {"Открыть кейс", "Мои кейсы"} for b in buttons)
    payload = CaseCB(action="account", key="9" * 18, token="a" * 24).pack()
    assert len(payload.encode()) <= 64
    assert CaseCB.unpack("case:open:0:-").action == "open"


async def test_support_menu_has_creation_history_and_back_navigation():
    buttons = [b for row in kb.menu().inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["Создать новое обращение", "Мои обращения", "В меню"]
    assert [b.callback_data for b in buttons] == ["case:open:0:-", "case:list:0:-", "cab:menu"]
    for markup in (kb.back(), kb.case_list([], 0, staff=False)):
        buttons = [b for row in markup.inline_keyboard for b in row]
        assert any(b.callback_data == "case:menu:0:-" for b in buttons)
    staff_buttons = [b for row in kb.back(staff=True).inline_keyboard for b in row]
    assert any(b.text == "В админку" for b in staff_buttons)
    assert not any(b.callback_data == "case:menu:0:-" for b in staff_buttons)


async def test_real_dispatcher_creation_preview_edit_submit_replies_cancel_and_acl(
    db_session, monkeypatch
):
    db_session.sync_session.expire_on_commit = False
    module = importlib.reload(support_router)
    dp = Dispatcher()
    dp.include_router(module.router)
    bot = Bot("123:abc")
    uid = 81001
    account = await seed(db_session, user_id=uid)
    other = await seed(db_session, user_id=81002, gross="99.99", exchange="bitget")
    methods = []

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
    number = 0

    def user(actor):
        return TelegramUser(id=actor, is_bot=False, first_name="Test", username="user_name")

    async def click(action, *, actor=uid, key="0", token=None):
        nonlocal number
        number += 1
        update = Update(
            update_id=number,
            callback_query=CallbackQuery(
                id=str(number),
                from_user=user(actor),
                chat_instance="test",
                data=CaseCB(action=action, key=key, token=token or "-").pack(),
                message=Message(
                    message_id=10,
                    date=datetime.now(UTC),
                    chat=Chat(id=actor, type="private"),
                    text="Panel",
                ),
            ),
        )
        await dp.feed_update(bot, update, session=db_session, admin_ids=ADMINS)

    async def text(body, *, actor=uid):
        nonlocal number
        number += 1
        update = Update(
            update_id=number,
            message=Message(
                message_id=number + 100,
                date=datetime.now(UTC),
                from_user=user(actor),
                chat=Chat(id=actor, type="private"),
                text=body,
            ),
        )
        await dp.feed_update(bot, update, session=db_session, admin_ids=ADMINS)

    state = dp.fsm.get_context(bot=bot, chat_id=uid, user_id=uid)
    try:
        await click("menu", actor=other.user_id)
        home = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert home.text == "<b>Техническая поддержка</b>"
        assert "Создать новое обращение" in str(home.reply_markup)
        assert "Мои обращения" in str(home.reply_markup)
        await click("list", actor=other.user_id)
        empty_list = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "Мои обращения" in empty_list.text and "Обращений пока нет" in empty_list.text
        assert "Создать новое обращение" in str(empty_list.reply_markup)
        await click("open", actor=other.user_id)
        denial = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "100 USDT" in denial.text and "99.99" in denial.text
        await click("open")
        context = await state.get_data()
        token = context["support_token"]
        exchange_panel = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "bitget" not in str(exchange_panel.reply_markup)
        await click("exchange", key="gate", token=token)
        await click("account", key=str(other.id), token=token)
        assert await state.get_state() == module.CaseFlow.account.state
        await click("account", key=str(account.id), token=token)
        await text("   ")
        assert await state.get_state() == module.CaseFlow.text.state
        await text("Вопрос <b>literal</b> & details")
        assert await state.get_state() == module.CaseFlow.preview.state
        preview = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "Предпросмотр" in preview.text and "&lt;b&gt;literal&lt;/b&gt;" in preview.text
        old_token = (await state.get_data())["support_token"]
        await click("edit", token=old_token)
        await text("Исправленный вопрос")
        new_token = (await state.get_data())["support_token"]
        await click("send", token=old_token)
        assert await db_session.scalar(select(func.count()).select_from(SupportCase)) == 0
        await click("send", token=new_token)
        await click("send", token=new_token)
        assert await db_session.scalar(select(func.count()).select_from(SupportCase)) == 1
        first = await db_session.scalar(select(SupportMessage))
        assert first.body == "Исправленный вопрос"
        case = await db_session.scalar(select(SupportCase))
        assert case.status == statuses.SENT
        await click("view", actor=9001, key=str(case.id))
        assert case.status == statuses.SENT
        card = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "Отправлено" in card.text and "Взять в работу" in str(card.reply_markup)
        await click("take", key=str(case.id))
        assert methods[-1].show_alert is True and case.status == statuses.SENT
        await click("take", actor=9001, key=str(case.id))
        assert case.status == statuses.OPEN

        await click("admin", actor=other.user_id)
        assert methods[-1].show_alert is True
        await click("view", actor=other.user_id, key=str(case.id))
        assert methods[-1].show_alert is True
        await click("list")
        own_list = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "Мои обращения" in own_list.text
        assert str(case.id) in str(own_list.reply_markup)
        await click("reply", key=str(case.id))
        await click("menu")
        assert await state.get_state() is None and "support_token" not in await state.get_data()
        await click("reply", actor=9001, key=str(case.id))
        await text("Ответ поддержки <safe>", actor=9001)
        staff_state = dp.fsm.get_context(bot=bot, chat_id=9001, user_id=9001)
        await click("send", actor=9001, token=(await staff_state.get_data())["support_token"])
        assert await db_session.scalar(select(func.count()).select_from(SupportMessage)) == 2
        assert case.status == statuses.REQUIRES_REPLY
        await click("view", key=str(case.id))
        card = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "Требуется ответ" in card.text and "&lt;safe&gt;" in card.text
        assert "Ответить" in str(card.reply_markup)
        first.created_at = datetime.now(UTC) - timedelta(minutes=2)
        await db_session.flush()
        await click("reply", key=str(case.id))
        await text("Ответ пользователя")
        await click("send", token=(await state.get_data())["support_token"])
        assert case.status == statuses.OPEN
        await click("thread", key=str(case.id), token="0")
        await click("message", key=str(first.id))

        await click("open")
        methods.clear()
        await click("cancel")
        assert await state.get_state() is None and await state.get_data() == {}
        assert any(isinstance(m, DeleteMessage) for m in methods)
        assert not any(isinstance(m, SendMessage | EditMessageText) for m in methods)

        await state.set_state("CredentialFlow:entering_credentials")
        await state.set_data({"panel_msg": 999, "credential_exchange": "bitget"})
        await click("cancel")
        assert await state.get_state() == "CredentialFlow:entering_credentials"
        assert (await state.get_data())["credential_exchange"] == "bitget"

        await click("closeask", actor=9001, key=str(case.id))
        assert case.closed_at is None
        await click("close", actor=9001, key=str(case.id))
        assert case.closed_at is not None
        assert case.status == statuses.CLOSED
        await click("admin", actor=9001, token="closed")
        archive = [m for m in methods if isinstance(m, EditMessageText)][-1]
        assert "Закрытые" in archive.text and "Кейс закрыт" in str(archive.reply_markup)
        await service.open_case(
            db_session,
            user_id=uid,
            account_id=account.id,
            body="Second case",
            token="f" * 24,
            admin_ids=ADMINS,
            now=datetime.now(UTC),
        )
        await click("open")
        assert methods[-1].show_alert is True
        assert "24 часа" in methods[-1].text and "2 обращения" in methods[-1].text
        assert await state.get_state() is None
        await click("list")
        assert "Мои обращения" in [m for m in methods if isinstance(m, EditMessageText)][-1].text
    finally:
        await bot.session.close()
        await dp.storage.close()
