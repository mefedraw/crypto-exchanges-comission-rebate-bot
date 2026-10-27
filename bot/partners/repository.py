"""Shared partner-account locking for accruals, rank decisions, and withdrawals."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.partners import PartnerAccount


async def lock_account(session: AsyncSession, user_id: int) -> PartnerAccount:
    await session.execute(insert(PartnerAccount).values(user_id=user_id).on_conflict_do_nothing())
    account = await session.scalar(
        select(PartnerAccount)
        .where(PartnerAccount.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise RuntimeError("Partner account could not be locked.")
    return account
