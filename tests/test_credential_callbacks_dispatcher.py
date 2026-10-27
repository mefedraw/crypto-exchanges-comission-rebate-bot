from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.methods import AnswerCallbackQuery, DeleteMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.config import Settings
from bot.handlers.credentials import (
    CredentialCB,
    CredentialFlow,
    _cancel_keyboard,
    _exchange_keyboard,
)
from bot.main import build_dispatcher


@pytest.mark.parametrize("payload", ["cred:cancel:", "cred:exchange:bitget"])
def test_existing_callback_payload_roundtrip(payload):
    assert CredentialCB.unpack(payload).pack() == payload


async def test_existing_cancel_buttons_reach_handler_via_production_dispatcher(monkeypatch):
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="123:abc",
        USER_TELEGRAM_IDS="111",
        ADMIN_TELEGRAM_IDS="111",
        DEVELOPER_TELEGRAM_ID=222,
        CREDENTIAL_UPDATE_ENABLED=False,
    )
    dp = build_dispatcher(settings)
    bot = Bot(token=settings.bot_token.get_secret_value())
    request = AsyncMock(return_value=True)
    monkeypatch.setattr(bot.session, "make_request", request)
    state = dp.fsm.get_context(bot=bot, chat_id=111, user_id=111)
    try:
        for index, (flow_state, keyboard) in enumerate(
            (
                (CredentialFlow.choosing_exchange, _exchange_keyboard()),
                (CredentialFlow.entering_uid, _exchange_keyboard()),
                (CredentialFlow.entering_credentials, _cancel_keyboard()),
            ),
            start=1,
        ):
            await state.set_state(flow_state)
            await state.set_data({"credential_exchange": "bitget"})
            request.reset_mock()
            payload = keyboard.inline_keyboard[-1][0].callback_data
            assert payload == "cred:cancel:"
            update = Update(
                update_id=index,
                callback_query=CallbackQuery(
                    id=f"cancel-probe-{index}",
                    from_user=User(id=111, is_bot=False, first_name="Admin"),
                    chat_instance="probe",
                    data=payload,
                    message=Message(
                        message_id=10 + index,
                        date=datetime.now(UTC),
                        chat=Chat(id=111, type="private"),
                        text="API credentials",
                        reply_markup=keyboard,
                    ),
                ),
            )

            result = await dp.feed_update(bot, update)

            assert result is not UNHANDLED, flow_state.state
            assert await state.get_state() is None
            assert await state.get_data() == {}
            methods = [call.args[1] for call in request.await_args_list]
            assert len(methods) == 2
            assert isinstance(methods[0], AnswerCallbackQuery)
            assert methods[0].text is None
            assert isinstance(methods[1], DeleteMessage)
            assert methods[1].message_id == 10 + index
    finally:
        await bot.session.close()
        await dp.storage.close()
        await dp["db_engine"].dispose()
