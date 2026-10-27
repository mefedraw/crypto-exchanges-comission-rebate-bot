from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from alembic import command
from alembic.config import Config
from bot.db.engine import make_engine


@pytest.mark.asyncio(loop_scope="session")
async def test_monthly_migration_preserves_source_and_earning_and_grants_protection(
    migrations_pg_url,
):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrations_pg_url)
    await asyncio.to_thread(command.upgrade, config, "e0369f84c125")
    engine = make_engine(migrations_pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users (id) VALUES (88001),(88002)"))
            await conn.execute(
                text(
                    "INSERT INTO exchange_accounts (id,user_id,exchange,uid,accrual_since) "
                    "VALUES (1,88002,'gate','88002',CURRENT_DATE-10)"
                )
            )
            await conn.execute(
                text("""
                INSERT INTO rebate_accruals
                (id,user_id,account_id,accrual_date,amount_usdt,amount_native,net_native,
                 effective_rank,fee_pct,commission_usdt,net_usdt,available_at,expires_at,finalized)
                VALUES (1,88002,1,CURRENT_DATE-1,100,100,90,1,.1,10,90,
                        now(),now()+interval '30 days',true)
            """)
            )
            await conn.execute(
                text("""
                INSERT INTO partner_earnings
                (source_accrual_id,referrer_id,invitee_id,policy_version,rank,share,asset,
                 owner_fee_usdt,owner_fee_native,amount_usdt,amount_native)
                VALUES (1,88001,88002,1,1,.2,'USDT',10,10,2,2)
            """)
            )
            before_source = (
                (await conn.execute(text("SELECT * FROM rebate_accruals WHERE id=1")))
                .mappings()
                .one()
            )
            before_earning = (
                (await conn.execute(text("SELECT * FROM partner_earnings"))).mappings().one()
            )
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as conn:
            after_source = (
                (await conn.execute(text("SELECT * FROM rebate_accruals WHERE id=1")))
                .mappings()
                .one()
            )
            after_earning = (
                (await conn.execute(text("SELECT * FROM partner_earnings"))).mappings().one()
            )
            assert dict(after_source) == dict(before_source)
            assert dict(after_earning) == dict(before_earning)
            account = (
                (await conn.execute(text("SELECT * FROM partner_accounts WHERE user_id=88001")))
                .mappings()
                .one()
            )
            assert account["current_rank"] == 1
            assert account["protected_until"] > account["promoted_at"]
            assert await conn.scalar(text("SELECT version FROM partner_program WHERE id=1")) == 2
        with pytest.raises(RuntimeError, match="history must be retained"):
            await asyncio.to_thread(command.downgrade, config, "e0369f84c125")
    finally:
        await engine.dispose()
