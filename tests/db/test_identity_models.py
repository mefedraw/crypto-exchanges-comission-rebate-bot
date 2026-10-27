from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount, User, Wallet

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mk_user(session: AsyncSession, uid: int = 100) -> User:
    user = User(id=uid, role="user", status="active")
    session.add(user)
    await session.flush()
    return user


async def test_user_round_trip(db_session: AsyncSession):
    await _mk_user(db_session, 101)
    got = (await db_session.execute(select(User).where(User.id == 101))).scalar_one()
    assert got.role == "user" and got.status == "active"


async def test_one_active_wallet_per_user(db_session: AsyncSession):
    await _mk_user(db_session, 102)
    db_session.add(
        Wallet(
            user_id=102,
            address_encrypted=b"a",
            address_hash=b"h1",
            address_masked="0x1..a",
            status="active",
        )
    )
    await db_session.flush()
    # a second ACTIVE wallet for the same user must violate the partial unique index
    db_session.add(
        Wallet(
            user_id=102,
            address_encrypted=b"b",
            address_hash=b"h2",
            address_masked="0x2..b",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_replaced_wallet_allows_new_active(db_session: AsyncSession):
    await _mk_user(db_session, 103)
    db_session.add(
        Wallet(
            user_id=103,
            address_encrypted=b"a",
            address_hash=b"h3",
            address_masked="0x..a",
            status="replaced",
        )
    )
    db_session.add(
        Wallet(
            user_id=103,
            address_encrypted=b"b",
            address_hash=b"h4",
            address_masked="0x..b",
            status="active",
        )
    )
    await db_session.flush()  # replaced + active is fine


async def test_wallet_requires_existing_user(db_session: AsyncSession):
    db_session.add(
        Wallet(
            user_id=999999,
            address_encrypted=b"x",
            address_hash=b"hx",
            address_masked="0x..x",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_exchange_account_requires_existing_user(db_session: AsyncSession):
    db_session.add(
        ExchangeAccount(
            user_id=999999,
            exchange="bybit",
            uid="UX",
            status="active",
            accrual_since=date(2026, 7, 10),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_exchange_uid_globally_unique(db_session: AsyncSession):
    await _mk_user(db_session, 104)
    await _mk_user(db_session, 105)
    db_session.add(
        ExchangeAccount(
            user_id=104,
            exchange="bybit",
            uid="U1",
            status="active",
            accrual_since=date(2026, 7, 10),
        )
    )
    await db_session.flush()
    db_session.add(
        ExchangeAccount(
            user_id=105,
            exchange="bybit",
            uid="U1",
            status="active",
            accrual_since=date(2026, 7, 10),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
