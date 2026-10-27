# tests/botui/test_menu.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import menu
from bot.botui import repository as repo
from bot.botui.states import WalletFlow

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _ctx(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _message(user_id: int, text: str = "/start") -> SimpleNamespace:
    return SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=user_id, username=None), answer=AsyncMock()
    )


async def test_start_upserts_user_and_shows_menu(db_session: AsyncSession):
    state = _ctx(940)
    msg = _message(940, "/start")

    await menu.open_cabinet(msg, state, db_session, frozenset())

    user = await repo.upsert_user(db_session, 940)
    assert user.id == 940
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    assert "reply_markup" in kwargs
    assert "Личный кабинет" in args[0]


async def test_cabinet_alias_upserts_user_and_shows_menu(db_session: AsyncSession):
    state = _ctx(941)
    msg = _message(941, "/cabinet")

    await menu.open_cabinet(msg, state, db_session, frozenset())

    assert await repo.upsert_user(db_session, 941) is not None
    msg.answer.assert_awaited_once()


async def test_start_promotes_env_listed_admin(db_session: AsyncSession):
    state = _ctx(944)
    msg = _message(944, "/start")

    await menu.open_cabinet(msg, state, db_session, frozenset({944}))

    user = await repo.upsert_user(db_session, 944)
    assert user.role == "admin"


async def test_start_leaves_non_admin_user_role_as_user(db_session: AsyncSession):
    state = _ctx(945)
    msg = _message(945, "/start")

    await menu.open_cabinet(msg, state, db_session, frozenset({944}))

    user = await repo.upsert_user(db_session, 945)
    assert user.role == "user"


async def test_cancel_clears_state_and_shows_menu(db_session: AsyncSession):
    await repo.upsert_user(db_session, 942)
    state = _ctx(942)
    await state.set_state(WalletFlow.waiting_address)
    msg = _message(942, "/cancel")

    await menu.cancel_to_menu(msg, state, db_session)

    assert await state.get_state() is None
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    assert "reply_markup" in kwargs


async def test_start_with_no_from_user_is_ignored(db_session: AsyncSession):
    state = _ctx(943)
    msg = SimpleNamespace(text="/start", from_user=None, answer=AsyncMock())

    await menu.open_cabinet(msg, state, db_session, frozenset())

    msg.answer.assert_not_awaited()


def test_router_dispatches_start_and_cabinet_to_open_cabinet() -> None:
    """Regression guard: a previous edit inserted `_show_mnt_button` between the
    `@router.message(CommandStart())` / `@router.message(Command("cabinet"))`
    decorators and `open_cabinet`, so the decorators silently registered the
    helper instead. Assert the router actually wires /start and /cabinet to
    `open_cabinet`, not to `_show_mnt_button`."""
    registered_callbacks = {handler.callback for handler in menu.router.message.handlers}

    assert menu.open_cabinet in registered_callbacks
    assert menu._show_mnt_button not in registered_callbacks

    for handler in menu.router.message.handlers:
        for filter_obj in handler.filters:
            cb = filter_obj.callback
            is_start = isinstance(cb, CommandStart)
            is_cabinet = isinstance(cb, Command) and "cabinet" in getattr(cb, "commands", ())
            if is_start or is_cabinet:
                assert handler.callback is menu.open_cabinet, (
                    f"{cb} is bound to " f"{handler.callback.__name__!r}, expected 'open_cabinet'"
                )
