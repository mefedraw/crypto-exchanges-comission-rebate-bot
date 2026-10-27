import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.ops import (
    ActionToken,
    AuditEvent,
    ServiceAvailability,
    SystemFlag,
    TelegramUpdate,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_telegram_update_dedup(db_session: AsyncSession):
    db_session.add(TelegramUpdate(update_id=9001))
    await db_session.flush()
    db_session.add(TelegramUpdate(update_id=9001))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_action_token_and_audit_and_flags(db_session: AsyncSession):
    db_session.add(
        ActionToken(
            token=uuid.uuid4(),
            user_id=None,
            action_type="withdraw",
            state="issued",
            expires_at=datetime(2026, 7, 13, tzinfo=UTC),
        )
    )
    db_session.add(
        AuditEvent(actor="system", action="payout.create", entity_type="payout", entity_id="1")
    )
    db_session.add(ServiceAvailability(down_from=datetime(2026, 7, 12, tzinfo=UTC)))
    db_session.add(SystemFlag(key="payouts_paused", value={"on": False}))
    await db_session.flush()
