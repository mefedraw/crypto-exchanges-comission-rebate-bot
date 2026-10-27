from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from testcontainers.postgres import PostgresContainer

from bot.db.base import Base
from bot.db.engine import make_engine


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def migrations_pg_url() -> Iterator[str]:
    """A fresh, empty Postgres container (no create_all) for migration round-trip tests."""
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def roundtrip_pg_url() -> Iterator[str]:
    """A separate, dedicated fresh Postgres container for the upgrade->downgrade->upgrade
    round-trip test, so it doesn't share state/ordering with `migrations_pg_url`."""
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest_asyncio.fixture(scope="session")
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Function-scoped session inside an external transaction that is rolled back
    after each test — survives commit() inside tests, so DB state never leaks."""
    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
