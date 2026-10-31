import asyncio

import pytest
from sqlalchemy import text

from alembic import command
from alembic.config import Config
from bot.db.engine import make_engine


@pytest.mark.asyncio(loop_scope="session")
async def test_fetch_table_upgrade_and_downgrade_preserve_existing_data(migrations_pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", migrations_pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "b369c217f458")
    engine = make_engine(migrations_pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users(id,username) VALUES(982001,'cache-test')"))
        await engine.dispose()
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM commission_fetches")) == 0
            assert (
                await conn.scalar(text("SELECT username FROM users WHERE id=982001"))
                == "cache-test"
            )
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, cfg, "b369c217f458")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('commission_fetches')")) is None
            assert (
                await conn.scalar(text("SELECT username FROM users WHERE id=982001"))
                == "cache-test"
            )
        await engine.dispose()
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await engine.dispose()
