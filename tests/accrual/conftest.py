# tests/accrual/conftest.py
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from testcontainers.postgres import PostgresContainer

from bot.db.base import Base
from bot.db.engine import make_engine


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest_asyncio.fixture(scope="session")
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clear_downtime_state(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    """Reset the shared heartbeat + downtime journal before each test.

    Worker tests commit (they don't use the transactional ``db_session``) and share
    the session-scoped DB, so the single global heartbeat row and any downtime
    windows would otherwise leak between tests and skew ``expire_aged`` grace. Only
    these two tables are cleared — accrual rows keep the unique-id isolation the
    existing tests rely on."""
    async with pg_engine.begin() as conn:
        await conn.execute(text("DELETE FROM service_availability"))
        await conn.execute(text("DELETE FROM system_flags WHERE key = 'accrual_heartbeat'"))
    yield


@pytest_asyncio.fixture
async def db_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
