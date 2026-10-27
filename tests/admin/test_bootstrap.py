# tests/admin/test_bootstrap.py
"""Admin bootstrap: env-listed users get role='admin' idempotently, others don't."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_ensure_role_promotes_env_listed_admin(db_session: AsyncSession) -> None:
    await repo.upsert_user(db_session, 9601)

    await repo.ensure_role(db_session, 9601, frozenset({9601}))

    user = await db_session.get(repo.User, 9601)
    assert user is not None
    assert user.role == "admin"


async def test_ensure_role_leaves_non_listed_user_as_user(db_session: AsyncSession) -> None:
    await repo.upsert_user(db_session, 9602)

    await repo.ensure_role(db_session, 9602, frozenset({9601}))

    user = await db_session.get(repo.User, 9602)
    assert user is not None
    assert user.role == "user"


async def test_ensure_role_is_idempotent(db_session: AsyncSession) -> None:
    await repo.upsert_user(db_session, 9603)

    await repo.ensure_role(db_session, 9603, frozenset({9603}))
    await repo.ensure_role(db_session, 9603, frozenset({9603}))

    user = await db_session.get(repo.User, 9603)
    assert user is not None
    assert user.role == "admin"


async def test_ensure_role_noop_with_empty_admin_set(db_session: AsyncSession) -> None:
    await repo.upsert_user(db_session, 9604)

    await repo.ensure_role(db_session, 9604, frozenset())

    user = await db_session.get(repo.User, 9604)
    assert user is not None
    assert user.role == "user"
