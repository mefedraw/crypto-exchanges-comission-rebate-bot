# tests/botui/test_action_token.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_consume_is_single_use(db_session: AsyncSession):
    await repo.upsert_user(db_session, 930)
    tok = uuid.uuid4()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    await repo.create_action_token(
        db_session,
        token=tok,
        user_id=930,
        action_type="withdraw",
        payload={"net": "45"},
        expires_at=now + timedelta(minutes=5),
    )
    first = await repo.consume_action_token(db_session, tok, now)
    second = await repo.consume_action_token(db_session, tok, now)
    assert first == {"net": "45"}
    assert second is None  # single use


async def test_consume_rejects_expired(db_session: AsyncSession):
    await repo.upsert_user(db_session, 931)
    tok = uuid.uuid4()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    await repo.create_action_token(
        db_session,
        token=tok,
        user_id=931,
        action_type="withdraw",
        payload={},
        expires_at=now - timedelta(seconds=1),  # already expired
    )
    assert await repo.consume_action_token(db_session, tok, now) is None
