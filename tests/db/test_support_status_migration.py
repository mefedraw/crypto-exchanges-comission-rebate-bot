from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from alembic import command
from alembic.config import Config
from bot.db.engine import make_engine


@pytest.mark.asyncio(loop_scope="session")
async def test_backfill_keeps_messages_and_maps_all_four_legacy_states(migrations_pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", migrations_pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "f147a095d236")
    engine = make_engine(migrations_pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users(id) VALUES (88101)"))
            for i, authors in enumerate(
                ((False,), (False, True, False), (False, True), (False, True))
            ):
                account_id = await conn.scalar(
                    text(
                        "INSERT INTO exchange_accounts(user_id,exchange,uid,accrual_since) "
                        "VALUES (88101,'gate',:uid,CURRENT_DATE) RETURNING id"
                    ),
                    {"uid": f"legacy-{i}"},
                )
                case_id = await conn.scalar(
                    text(
                        "INSERT INTO support_cases(user_id,account_id,exchange,uid,"
                        "gross_at_open,request_token,closed_at) "
                        "VALUES (88101,:account,'gate',:uid,100,:token,:closed) RETURNING id"
                    ),
                    {
                        "account": account_id,
                        "uid": f"legacy-{i}",
                        "token": f"{i:024x}",
                        "closed": None,
                    },
                )
                if i == 3:
                    await conn.execute(
                        text(
                            "UPDATE support_cases SET closed_at=now(),closed_by=9001 WHERE id=:id"
                        ),
                        {"id": case_id},
                    )
                for j, staff in enumerate(authors):
                    await conn.execute(
                        text(
                            "INSERT INTO support_messages"
                            "(case_id,author_id,is_staff,body,request_token) "
                            "VALUES (:case_id,:actor,:staff,:body,:token)"
                        ),
                        {
                            "case_id": case_id,
                            "actor": 9001 if staff else 88101,
                            "staff": staff,
                            "body": f"Message {i}/{j}",
                            "token": f"{i+10:012x}{j:012x}",
                        },
                    )
            before = [
                dict(r)
                for r in (
                    await conn.execute(text("SELECT * FROM support_cases ORDER BY id"))
                ).mappings()
            ]
            messages = [
                dict(r)
                for r in (
                    await conn.execute(text("SELECT * FROM support_messages ORDER BY id"))
                ).mappings()
            ]
        # Deployment recreates the bot, so do not reuse prepared SELECT * after DDL.
        await engine.dispose()
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.connect() as conn:
            after = [
                dict(r)
                for r in (
                    await conn.execute(text("SELECT * FROM support_cases ORDER BY id"))
                ).mappings()
            ]
            assert [r.pop("status") for r in after] == ["sent", "open", "requires_reply", "closed"]
            assert after == before
            assert [
                dict(r)
                for r in (
                    await conn.execute(text("SELECT * FROM support_messages ORDER BY id"))
                ).mappings()
            ] == messages
        with pytest.raises(RuntimeError, match="status history must be retained"):
            await asyncio.to_thread(command.downgrade, cfg, "f147a095d236")
    finally:
        await engine.dispose()
