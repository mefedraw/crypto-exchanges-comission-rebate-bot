# tests/botui/test_service_accounts.py
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo
from bot.botui import service
from bot.db.models.ops import AuditEvent
from bot.exchanges.base import ReferralCheck

pytestmark = pytest.mark.asyncio(loop_scope="session")

EPOCH = date(2026, 7, 10)


async def _audit_count(session: AsyncSession, action: str) -> int:
    stmt = select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action)
    return (await session.scalar(stmt)) or 0


def _verifier(verdict: ReferralCheck) -> service.ReferralVerifier:
    async def _v(exchange: str, uid: str) -> ReferralCheck:
        return verdict

    return _v


async def test_add_uid_persists_and_writes_audit(db_session: AsyncSession):
    await repo.upsert_user(db_session, 930)
    result = await service.add_uid(db_session, 930, "gate", "UID930", EPOCH)
    assert result.outcome == "added"
    assert result.account is not None
    assert result.account.exchange == "gate"
    assert result.account.uid == "UID930"
    # No verifier injected -> the add proceeds as unverifiable (never blocks).
    assert result.status is ReferralCheck.UNVERIFIABLE
    assert await _audit_count(db_session, "uid_added") == 1


async def test_add_uid_collision_returns_collision_and_writes_audit(db_session: AsyncSession):
    await repo.upsert_user(db_session, 931)
    await repo.upsert_user(db_session, 932)
    first = await service.add_uid(db_session, 931, "kucoin", "DUPUID", EPOCH)
    assert first.account is not None
    second = await service.add_uid(db_session, 932, "kucoin", "DUPUID", EPOCH)
    assert second.outcome == "collision"
    assert second.account is None
    assert second.held_by == 931  # the current holder, surfaced for the owner's alert
    assert await _audit_count(db_session, "uid_collision") == 1


async def test_unbind_is_reversible_for_owner_but_not_others(db_session: AsyncSession):
    await repo.upsert_user(db_session, 935)
    await repo.upsert_user(db_session, 936)
    added = await service.add_uid(db_session, 935, "gate", "REVUID", EPOCH)
    assert added.account is not None
    # 935 unbinds it.
    assert await repo.archive_account(db_session, 935, added.account.id) is True

    # Another user STILL cannot claim the archived UID (anti-theft): collision, alert.
    other = await service.add_uid(db_session, 936, "gate", "REVUID", EPOCH)
    assert other.outcome == "collision"
    assert other.held_by == 935

    # The original owner CAN re-add it — the archived row is resurrected (same account).
    back = await service.add_uid(db_session, 935, "gate", "REVUID", EPOCH)
    assert back.outcome == "added"
    assert back.account is not None
    assert back.account.id == added.account.id and back.account.status == "active"
    assert len(await repo.list_accounts(db_session, 935)) == 1


async def test_add_uid_rejects_bad_uid(db_session: AsyncSession):
    await repo.upsert_user(db_session, 933)
    with pytest.raises(service.UidError):
        await service.add_uid(db_session, 933, "gate", "bad uid!", EPOCH)


async def test_add_uid_rejects_unknown_exchange(db_session: AsyncSession):
    await repo.upsert_user(db_session, 934)
    with pytest.raises(service.UidError):
        await service.add_uid(db_session, 934, "notreal", "UID934", EPOCH)


async def test_add_uid_confirmed_referral_added(db_session: AsyncSession):
    await repo.upsert_user(db_session, 935)
    result = await service.add_uid(
        db_session, 935, "gate", "UID935", EPOCH, verifier=_verifier(ReferralCheck.CONFIRMED)
    )
    assert result.outcome == "added"
    assert result.status is ReferralCheck.CONFIRMED
    assert result.account is not None


async def test_add_uid_not_referral_is_rejected_and_not_persisted(db_session: AsyncSession):
    await repo.upsert_user(db_session, 936)
    result = await service.add_uid(
        db_session, 936, "gate", "STRANGER", EPOCH, verifier=_verifier(ReferralCheck.NOT_REFERRAL)
    )
    assert result.outcome == "not_referral"
    assert result.account is None
    assert await repo.list_accounts(db_session, 936) == []
    assert await _audit_count(db_session, "uid_rejected_not_referral") == 1


async def test_add_uid_unverifiable_still_added(db_session: AsyncSession):
    await repo.upsert_user(db_session, 937)
    result = await service.add_uid(
        db_session, 937, "weex", "UID937", EPOCH, verifier=_verifier(ReferralCheck.UNVERIFIABLE)
    )
    assert result.outcome == "added"
    assert result.status is ReferralCheck.UNVERIFIABLE
    assert result.account is not None
