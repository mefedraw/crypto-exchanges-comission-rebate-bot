from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bot.botui.middleware import DbSessionMiddleware

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_commits_on_success(pg_engine: AsyncEngine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    mw = DbSessionMiddleware(sm)
    seen = {}

    async def handler(event, data):
        await data["session"].execute(text("SELECT 1"))
        seen["had_session"] = data["session"] is not None
        return "ok"

    result = await mw(handler, object(), {})
    assert result == "ok"
    assert seen["had_session"] is True


async def test_rolls_back_and_reraises_on_error(pg_engine: AsyncEngine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    mw = DbSessionMiddleware(sm)

    async def handler(event, data):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await mw(handler, object(), {})
