# tests/test_username_resolve.py
"""Username capture + @nick -> user_id resolution (foundation for admin rate-by-nickname).

Deliberately a FLAT file (not tests/botui/test_username_resolve.py or a new
tests/<pkg>/ dir) per the task-11 brief. Because of that, its Postgres fixtures are
self-contained here rather than imported from tests/botui/conftest.py or centralized
in tests/conftest.py — see tests/test_worker_mnt.py's module docstring for the
full explanation: this file sorts alphabetically after every package directory but
among the OTHER flat tests/test_*.py files (most of which run under the default
per-test event loop, no loop_scope override). A session-scoped async engine created
under pytest-asyncio's "session" loop, then reused after intervening
default-scoped tests have each spun up and torn down their own loop, ends up bound
to a stale loop — asyncpg then raises "got Future ... attached to a different
loop". A module-scoped container + FUNCTION-scoped engine per test (cheap:
create_all is a no-op past the first call) sidesteps the whole cross-test
loop-lifecycle question, matching test_worker_mnt.py's pattern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from testcontainers.postgres import PostgresContainer

from bot.botui import repository as repo
from bot.db.base import Base
from bot.db.engine import make_engine

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest_asyncio.fixture(loop_scope="function")
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def db_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


async def test_upsert_user_stores_lowercased_username(db_session: AsyncSession):
    await repo.upsert_user(db_session, 42, username="Whale")
    assert await repo.find_user_id_by_username(db_session, "@whale") == 42
    assert await repo.find_user_id_by_username(db_session, "WHALE") == 42


async def test_find_user_id_by_username_unknown_returns_none(db_session: AsyncSession):
    assert await repo.find_user_id_by_username(db_session, "nobody") is None


async def test_upsert_user_without_username_does_not_wipe_existing(db_session: AsyncSession):
    await repo.upsert_user(db_session, 43, username="Whale")
    await repo.upsert_user(db_session, 43)  # returning user, no username on this update
    assert await repo.find_user_id_by_username(db_session, "whale") == 43


async def test_upsert_user_clears_username_from_stale_holder_on_reuse(
    db_session: AsyncSession,
) -> None:
    """FIX M2: Telegram usernames are unique only at any instant. User A ("bob")
    can drop the handle; if user B later takes "@bob" and messages the bot, A's
    stale row must be cleared so ``find_user_id_by_username`` resolves to B, not
    the former (now-wrong) owner."""
    await repo.upsert_user(db_session, 44, username="bob")
    assert await repo.find_user_id_by_username(db_session, "bob") == 44

    await repo.upsert_user(db_session, 45, username="bob")

    assert await repo.find_user_id_by_username(db_session, "bob") == 45
    user_a = await db_session.get(repo.User, 44)
    assert user_a is not None and user_a.username is None
