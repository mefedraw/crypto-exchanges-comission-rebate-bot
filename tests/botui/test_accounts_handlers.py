# tests/botui/test_accounts_handlers.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import accounts
from bot.botui import repository as repo
from bot.botui.keyboards import UidExchangeCB, UidRemoveCB, UidRemoveConfirmCB
from bot.botui.states import UidFlow
from bot.config import get_settings
from bot.exchanges.base import ReferralCheck

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _patch_verifier(monkeypatch, verdict: ReferralCheck) -> None:
    """Stub the network referral check so handler tests never hit an exchange."""

    async def _fake(settings, exchange, uid):
        return verdict

    monkeypatch.setattr("bot.botui.referral.verify_referral", _fake)


def _ctx(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _panel(chat_id: int = 500) -> MagicMock:
    # spec=Message so isinstance(callback.message, Message) passes. The live-panel UX edits
    # in place, so edit_text/answer/delete are awaitable and chat.id/message_id are real.
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    message.chat = SimpleNamespace(id=chat_id)
    message.message_id = 111
    return message


async def test_list_uids_edits_panel_and_sets_state(db_session: AsyncSession):
    await repo.upsert_user(db_session, 940)
    state = _ctx(940)
    message = _panel()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=940), answer=AsyncMock()
    )
    await accounts.list_uids(callback, state, db_session)  # type: ignore[arg-type]
    assert await state.get_state() == UidFlow.choosing_exchange.state
    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()  # edited in place, not a new message
    assert (await state.get_data())["panel_msg"] == 111


async def test_choosing_exchange_stores_code_and_prompts(db_session: AsyncSession):
    await repo.upsert_user(db_session, 941)
    state = _ctx(941)
    await state.set_state(UidFlow.choosing_exchange)
    message = _panel()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=941), answer=AsyncMock()
    )
    await accounts.choose_exchange(callback, UidExchangeCB(code="gate"), state)  # type: ignore[arg-type]
    assert await state.get_state() == UidFlow.waiting_uid.state
    assert (await state.get_data())["exchange"] == "gate"
    message.edit_text.assert_awaited()


async def test_valid_uid_deletes_input_and_returns_to_list(db_session: AsyncSession, monkeypatch):
    _patch_verifier(monkeypatch, ReferralCheck.CONFIRMED)
    await repo.upsert_user(db_session, 942)
    state = _ctx(942)
    await state.set_state(UidFlow.waiting_uid)
    await state.update_data(exchange="gate", panel_chat=942, panel_msg=1)
    bot = AsyncMock()
    alerter = AsyncMock()
    msg = SimpleNamespace(
        text="UID942", from_user=SimpleNamespace(id=942), answer=AsyncMock(), delete=AsyncMock()
    )
    await accounts.receive_uid(msg, state, db_session, bot, alerter)  # type: ignore[arg-type]
    # returns to the list (choosing_exchange) so the next exchange can be tapped directly
    assert await state.get_state() == UidFlow.choosing_exchange.state
    accounts_list = await repo.list_accounts(db_session, 942)
    assert len(accounts_list) == 1 and accounts_list[0].uid == "UID942"
    msg.delete.assert_awaited()  # typed UID erased
    bot.edit_message_text.assert_awaited()  # panel refreshed in place
    assert "подтверждён" in bot.edit_message_text.await_args.args[0]


async def test_unverifiable_uid_added_with_warning(db_session: AsyncSession, monkeypatch):
    _patch_verifier(monkeypatch, ReferralCheck.UNVERIFIABLE)
    await repo.upsert_user(db_session, 947)
    state = _ctx(947)
    await state.set_state(UidFlow.waiting_uid)
    await state.update_data(exchange="weex", panel_chat=947, panel_msg=1)
    bot = AsyncMock()
    alerter = AsyncMock()
    msg = SimpleNamespace(
        text="UID947", from_user=SimpleNamespace(id=947), answer=AsyncMock(), delete=AsyncMock()
    )
    await accounts.receive_uid(msg, state, db_session, bot, alerter)  # type: ignore[arg-type]
    assert len(await repo.list_accounts(db_session, 947)) == 1  # added despite unverifiable
    assert "не подтверждено" in bot.edit_message_text.await_args.args[0]


async def test_not_referral_uid_rejected(db_session: AsyncSession, monkeypatch):
    _patch_verifier(monkeypatch, ReferralCheck.NOT_REFERRAL)
    await repo.upsert_user(db_session, 948)
    state = _ctx(948)
    await state.set_state(UidFlow.waiting_uid)
    await state.update_data(exchange="gate", panel_chat=948, panel_msg=1)
    bot = AsyncMock()
    alerter = AsyncMock()
    msg = SimpleNamespace(
        text="STRANGER", from_user=SimpleNamespace(id=948), answer=AsyncMock(), delete=AsyncMock()
    )
    await accounts.receive_uid(msg, state, db_session, bot, alerter)  # type: ignore[arg-type]
    assert await repo.list_accounts(db_session, 948) == []  # rejected, not persisted
    msg.delete.assert_awaited()
    assert "не найден" in bot.edit_message_text.await_args.args[0]


async def test_collision_uid_reports_taken_in_panel(db_session: AsyncSession, monkeypatch):
    _patch_verifier(monkeypatch, ReferralCheck.CONFIRMED)
    await repo.upsert_user(db_session, 943)
    await repo.upsert_user(db_session, 944)
    await repo.add_account(db_session, 943, "gate", "TAKEN", get_settings().rebate_epoch)
    state = _ctx(944)
    await state.set_state(UidFlow.waiting_uid)
    await state.update_data(exchange="gate", panel_chat=944, panel_msg=1)
    bot = AsyncMock()
    alerter = AsyncMock()
    msg = SimpleNamespace(
        text="TAKEN", from_user=SimpleNamespace(id=944), answer=AsyncMock(), delete=AsyncMock()
    )
    await accounts.receive_uid(msg, state, db_session, bot, alerter)  # type: ignore[arg-type]
    assert await repo.list_accounts(db_session, 944) == []
    bot.edit_message_text.assert_awaited()
    assert "занят" in bot.edit_message_text.await_args.args[0]
    # the owner is alerted so they can decide a reassignment
    alerter.send.assert_awaited()
    assert "943" in alerter.send.await_args.args[1]  # current holder in the alert detail


async def test_bad_uid_shows_error_returns_to_list(db_session: AsyncSession, monkeypatch):
    _patch_verifier(monkeypatch, ReferralCheck.CONFIRMED)
    await repo.upsert_user(db_session, 945)
    state = _ctx(945)
    await state.set_state(UidFlow.waiting_uid)
    await state.update_data(exchange="gate", panel_chat=945, panel_msg=1)
    bot = AsyncMock()
    alerter = AsyncMock()
    msg = SimpleNamespace(
        text="bad uid!", from_user=SimpleNamespace(id=945), answer=AsyncMock(), delete=AsyncMock()
    )
    await accounts.receive_uid(msg, state, db_session, bot, alerter)  # type: ignore[arg-type]
    assert await state.get_state() == UidFlow.choosing_exchange.state
    assert await repo.list_accounts(db_session, 945) == []
    msg.delete.assert_awaited()


async def test_remove_is_two_step_confirm_then_archive(db_session: AsyncSession):
    await repo.upsert_user(db_session, 946)
    account = await repo.add_account(db_session, 946, "gate", "UID946", get_settings().rebate_epoch)
    assert account is not None
    state = _ctx(946)

    # Step 1: tapping ❌ only shows a confirmation — nothing is archived yet.
    message = _panel()
    cb1 = SimpleNamespace(message=message, from_user=SimpleNamespace(id=946), answer=AsyncMock())
    await accounts.confirm_remove_prompt(cb1, UidRemoveCB(account_id=account.id), state, db_session)  # type: ignore[arg-type]
    assert len(await repo.list_accounts(db_session, 946)) == 1  # still bound
    assert "Отвязать" in message.edit_text.await_args.args[0]

    # Step 2: confirming archives it (reversibly).
    message2 = _panel()
    cb2 = SimpleNamespace(message=message2, from_user=SimpleNamespace(id=946), answer=AsyncMock())
    await accounts.do_remove_uid(cb2, UidRemoveConfirmCB(account_id=account.id), state, db_session)  # type: ignore[arg-type]
    assert await repo.list_accounts(db_session, 946) == []
    cb2.answer.assert_awaited()
    message2.edit_text.assert_awaited()
    assert await state.get_state() == UidFlow.choosing_exchange.state

    # Reversible: the same owner can re-add (resurrect) their own UID.
    again = await repo.add_account(db_session, 946, "gate", "UID946", get_settings().rebate_epoch)
    assert again is not None and again.status == "active"
    assert len(await repo.list_accounts(db_session, 946)) == 1
