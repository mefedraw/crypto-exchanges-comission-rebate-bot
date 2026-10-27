import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_session_executes_query(db_session: AsyncSession):
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar_one() == 1
