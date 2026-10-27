# tests/botui/test_repository_identity.py
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo

pytestmark = pytest.mark.asyncio(loop_scope="session")

EPOCH = date(2026, 7, 10)


async def test_upsert_user_is_idempotent(db_session: AsyncSession):
    u1 = await repo.upsert_user(db_session, 900)
    u2 = await repo.upsert_user(db_session, 900)
    assert u1.id == u2.id == 900
    assert u1.role == "user"


async def test_replace_wallet_keeps_one_active(db_session: AsyncSession):
    await repo.upsert_user(db_session, 901)
    await repo.replace_wallet(
        db_session, 901, address_encrypted=b"enc1", address_hash=b"h1", address_masked="0x1..1"
    )
    w2 = await repo.replace_wallet(
        db_session, 901, address_encrypted=b"enc2", address_hash=b"h2", address_masked="0x2..2"
    )
    active = await repo.get_active_wallet(db_session, 901)
    assert active is not None and active.id == w2.id and active.address_masked == "0x2..2"


async def test_add_account_collision_returns_none(db_session: AsyncSession):
    await repo.upsert_user(db_session, 902)
    await repo.upsert_user(db_session, 903)
    a = await repo.add_account(db_session, 902, "bybit", "UID1", EPOCH)
    assert a is not None
    dup = await repo.add_account(db_session, 903, "bybit", "UID1", EPOCH)
    assert dup is None  # taken by 902, first-come


async def test_list_and_archive_accounts(db_session: AsyncSession):
    await repo.upsert_user(db_session, 904)
    a = await repo.add_account(db_session, 904, "okx", "U904", EPOCH)
    assert a is not None
    assert len(await repo.list_accounts(db_session, 904)) == 1
    assert await repo.archive_account(db_session, 904, a.id) is True
    assert await repo.list_accounts(db_session, 904) == []
