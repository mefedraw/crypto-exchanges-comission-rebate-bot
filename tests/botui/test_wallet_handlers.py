# tests/botui/test_wallet_handlers.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo
from bot.botui import service, wallet
from bot.botui.states import WalletFlow
from bot.security.secrets import SecretCipher

pytestmark = pytest.mark.asyncio(loop_scope="session")

GOOD = "0x" + "cd" * 20


def _ctx(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _panel() -> MagicMock:
    # spec=Message so isinstance passes; the live-panel UX edits in place.
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.chat = SimpleNamespace(id=1)
    message.message_id = 1
    return message


def _typed(text: str, user_id: int) -> SimpleNamespace:
    # A message the user typed: it gets deleted, and the panel is edited (or, with no
    # tracked panel, answered).
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id),
        message_id=2,
        answer=AsyncMock(),
        delete=AsyncMock(),
    )


async def test_prompt_shows_current_wallet(db_session: AsyncSession):
    await repo.upsert_user(db_session, 930)
    cipher = SecretCipher(Fernet.generate_key().decode())
    await service.set_wallet(db_session, cipher, 930, GOOD)
    state = _ctx(930)
    message = _panel()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=930), answer=AsyncMock()
    )
    await wallet.prompt_wallet(callback, state, db_session)  # type: ignore[arg-type]
    assert await state.get_state() == WalletFlow.waiting_address.state
    text = message.edit_text.await_args.args[0]  # panel edited in place
    assert service.mask_address(GOOD) in text  # current wallet shown as start…end
    callback.answer.assert_awaited()


async def test_prompt_no_wallet_yet(db_session: AsyncSession):
    await repo.upsert_user(db_session, 931)
    state = _ctx(931)
    message = _panel()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=931), answer=AsyncMock()
    )
    await wallet.prompt_wallet(callback, state, db_session)  # type: ignore[arg-type]
    assert "не задан" in message.edit_text.await_args.args[0]


async def test_good_address_persists_and_clears(db_session: AsyncSession):
    await repo.upsert_user(db_session, 920)
    cipher = SecretCipher(Fernet.generate_key().decode())
    state = _ctx(920)
    await state.set_state(WalletFlow.waiting_address)
    msg = _typed(GOOD, 920)
    await wallet.receive_wallet(msg, state, db_session, cipher, AsyncMock())
    assert await state.get_state() is None
    assert await repo.get_active_wallet(db_session, 920) is not None
    msg.delete.assert_awaited()  # typed address removed
    msg.answer.assert_awaited()  # no tracked panel -> result posted as fresh panel


async def test_bad_address_reprompts_no_wallet(db_session: AsyncSession):
    await repo.upsert_user(db_session, 921)
    cipher = SecretCipher(Fernet.generate_key().decode())
    state = _ctx(921)
    await state.set_state(WalletFlow.waiting_address)
    msg = _typed("nope", 921)
    await wallet.receive_wallet(msg, state, db_session, cipher, AsyncMock())
    assert await state.get_state() == WalletFlow.waiting_address.state
    assert await repo.get_active_wallet(db_session, 921) is None
