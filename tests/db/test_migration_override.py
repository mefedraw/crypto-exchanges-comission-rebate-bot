"""Round-trip test for the `users` commission-override columns migration
(469b86059887): upgrade adds them, downgrade -1 drops them."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import inspect

from alembic import command
from alembic.config import Config
from bot.db.engine import make_engine


async def _columns(pg_url: str) -> set[str]:
    def _cols(sync_conn):
        return {c["name"] for c in inspect(sync_conn).get_columns("users")}

    engine = make_engine(pg_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_cols)
    finally:
        await engine.dispose()


@pytest.mark.asyncio(loop_scope="session")
async def test_upgrade_adds_and_downgrade_drops_override_columns(
    migrations_pg_url: str,
) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", migrations_pg_url)

    await asyncio.to_thread(command.upgrade, cfg, "head")
    cols = await _columns(migrations_pg_url)
    assert {"commission_override_pct", "commission_override_since"} <= cols

    # Land exactly on the override migration (regardless of what head is now that
    # later migrations chain after it), then step one below it to drop the columns.
    await asyncio.to_thread(command.downgrade, cfg, "469b86059887")
    await asyncio.to_thread(command.downgrade, cfg, "-1")
    cols = await _columns(migrations_pg_url)
    assert "commission_override_pct" not in cols
    assert "commission_override_since" not in cols

    # leave the DB at head so it doesn't interfere with other tests sharing
    # this module-scoped container/fixture.
    await asyncio.to_thread(command.upgrade, cfg, "head")
