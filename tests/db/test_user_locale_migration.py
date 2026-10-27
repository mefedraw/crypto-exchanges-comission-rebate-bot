from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from alembic import command
from alembic.config import Config
from bot.db.engine import make_engine


@pytest.mark.asyncio(loop_scope="session")
async def test_locale_upgrade_retains_existing_users_and_is_backward_compatible(migrations_pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", migrations_pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "a258b106e347")
    engine = make_engine(migrations_pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users(id, username) VALUES (819001,'existing')"))
            before = dict(
                (await conn.execute(text("SELECT * FROM users WHERE id=819001"))).mappings().one()
            )
        await engine.dispose()
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.begin() as conn:
            after = dict(
                (await conn.execute(text("SELECT * FROM users WHERE id=819001"))).mappings().one()
            )
            assert after.pop("locale") == "ru"
            assert after == before
            await conn.execute(text("INSERT INTO users(id) VALUES (819002)"))
            assert await conn.scalar(text("SELECT locale FROM users WHERE id=819002")) == "ru"
            await conn.execute(text("UPDATE users SET locale='hi' WHERE id=819001"))
            assert await conn.scalar(text("SELECT locale FROM users WHERE id=819001")) == "hi"
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, cfg, "a258b106e347")
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await engine.dispose()
