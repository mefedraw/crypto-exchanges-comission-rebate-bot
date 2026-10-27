"""Migration round-trip test: `alembic upgrade head` builds the whole schema
on a fresh, empty Postgres container (no create_all involved)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import inspect, text

from alembic import command
from alembic.config import Config
from bot.db.engine import make_engine


@pytest.mark.asyncio(loop_scope="session")
async def test_upgrade_creates_all_tables(migrations_pg_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", migrations_pg_url)
    # alembic/env.py drives its own asyncio.run() internally; command.upgrade must
    # therefore run off-loop (in a worker thread), or it collides with the loop
    # that pytest-asyncio is already running for this test.
    await asyncio.to_thread(command.upgrade, cfg, "head")

    def _tables(sync_conn):
        return set(inspect(sync_conn).get_table_names())

    engine = make_engine(migrations_pg_url)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(_tables)
            assert await conn.scalar(
                text(
                    "SELECT starts_on = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date + 1 "
                    "FROM partner_program WHERE id = 1"
                )
            )
    finally:
        await engine.dispose()

    expected = {
        "users",
        "wallets",
        "exchange_accounts",
        "rebate_accruals",
        "rank_history",
        "payouts",
        "audit_events",
        "telegram_updates",
        "action_tokens",
        "service_availability",
        "system_flags",
        "partner_program",
        "partner_accounts",
        "partner_earnings",
        "partner_rank_reviews",
        "alembic_version",
    }
    assert expected <= tables


@pytest.mark.asyncio(loop_scope="session")
async def test_upgrade_downgrade_upgrade_roundtrip(roundtrip_pg_url: str) -> None:
    """upgrade -> downgrade -> upgrade must not raise. A leaked ENUM type from a
    prior downgrade would make the second upgrade fail with
    'type "..." already exists'."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", roundtrip_pg_url)

    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")

    def _tables(sync_conn):
        return set(inspect(sync_conn).get_table_names())

    engine = make_engine(roundtrip_pg_url)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(_tables)
    finally:
        await engine.dispose()

    expected = {
        "users",
        "wallets",
        "exchange_accounts",
        "rebate_accruals",
        "rank_history",
        "payouts",
        "audit_events",
        "telegram_updates",
        "action_tokens",
        "service_availability",
        "system_flags",
        "partner_program",
        "partner_accounts",
        "partner_earnings",
        "partner_rank_reviews",
        "alembic_version",
    }
    assert expected <= tables
