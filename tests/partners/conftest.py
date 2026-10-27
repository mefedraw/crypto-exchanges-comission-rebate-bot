import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from bot.db.base import Base
from bot.db.engine import make_engine
from tests.botui.conftest import db_session, pg_engine, pg_url  # noqa: F401


@pytest_asyncio.fixture
async def committed_engine():
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        engine = make_engine(pg.get_connection_url())
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            yield engine
        finally:
            await engine.dispose()
