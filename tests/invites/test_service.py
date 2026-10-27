from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.invites import InviteAttribution, InviteCode
from bot.db.models.ops import AuditEvent
from bot.invites import service

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


async def seed(session, user_id=82001):
    session.add(User(id=user_id, role="user", status="active"))
    await session.flush()
    return await service.current_code(session, user_id)


async def register(session, user_id, code=None):
    return await service.register_start(
        session,
        user_id=user_id,
        username=None,
        payload="r_" + code if code is not None else None,
        now=NOW,
    )


async def test_generated_code_is_opaque_stable_and_user_specific(db_session):
    one = await seed(db_session)
    two = await seed(db_session, 82002)
    assert re.fullmatch("[a-z0-9]{5}", one.code)
    assert any(c.isdigit() for c in one.code) and any(c.isalpha() for c in one.code)
    assert one.code != str(one.user_id) and one.code != two.code
    assert (await service.current_code(db_session, one.user_id)).id == one.id


@pytest.mark.parametrize(
    "raw,expected",
    [("Custom_CODE", "custom_code"), (" my-brand ", "my-brand"), ("a" * 32, "a" * 32)],
)
async def test_code_normalization(raw, expected):
    assert service.normalize_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abcd",
        "a" * 33,
        "вася123",
        "Kabcd",
        "a bcd",
        "a:bcd",
        "../other",
        "admin",
        "SUPPORT",
        "abc\nxyz",
    ],
)
async def test_invalid_or_reserved_codes_rejected(raw):
    with pytest.raises(service.InviteError):
        service.normalize_code(raw)


@pytest.mark.parametrize("legacy", ["a" * 24, "my_old-code", "12345", "abcde"])
async def test_legacy_code_upgrades_and_old_links_remain_valid(db_session, legacy):
    db_session.add(User(id=82001, role="user", status="active"))
    await db_session.flush()
    old = InviteCode(user_id=82001, code=legacy)
    db_session.add(old)
    await db_session.flush()
    old_id = old.id
    assert (await register(db_session, 82002, legacy)).outcome == "invited"
    new = await service.current_code(db_session, 82001)
    assert new.id != old_id and service._is_short_code(new.code)
    assert (await service.current_code(db_session, 82001)).id == new.id
    assert (await register(db_session, 82003, legacy)).outcome == "invited"
    assert (await register(db_session, 82004, new.code)).outcome == "invited"
    assert (await db_session.get(InviteAttribution, 82002)).code_id == old_id
    assert (await db_session.get(InviteAttribution, 82003)).code_id == old_id
    assert (await db_session.get(InviteAttribution, 82004)).referrer_id == 82001
    assert await db_session.scalar(select(func.count()).select_from(InviteCode)) == 2


async def test_attribution_is_one_time_and_no_self_or_cycles(db_session):
    parent = await seed(db_session)
    assert (await register(db_session, 82002, parent.code)).outcome == "invited"
    child = await service.current_code(db_session, 82002)
    assert (await register(db_session, 82001, child.code)).outcome == "existing"
    assert (await register(db_session, 82002, child.code)).outcome == "self"
    assert (await register(db_session, 82002, parent.code)).outcome == "existing"
    assert await db_session.get(InviteAttribution, 82001) is None
    assert (await db_session.get(InviteAttribution, 82002)).referrer_id == 82001
    assert await db_session.scalar(select(func.count()).select_from(InviteAttribution)) == 1


async def test_existing_user_without_referrer_cannot_be_claimed(db_session):
    code = await seed(db_session)
    assert (await register(db_session, 82002)).created
    assert (await register(db_session, 82002, code.code)).outcome == "existing"
    assert await db_session.get(InviteAttribution, 82002) is None


async def test_invalid_link_still_registers_without_attribution(db_session):
    result = await register(db_session, 82002, "not-a-real-code")
    assert result.created and result.outcome == "invalid"
    assert await db_session.get(User, 82002) is not None
    assert await db_session.get(InviteAttribution, 82002) is None


async def test_blocked_referrer_does_not_receive_attribution(db_session):
    code = await seed(db_session)
    user = await db_session.get(User, 82001)
    user.status = "blocked"
    await db_session.flush()
    assert (await register(db_session, 82002, code.code)).outcome == "invalid"
    with pytest.raises(service.InviteError):
        await service.current_code(db_session, 82001)


async def test_counts_do_not_duplicate_invitees_with_multiple_accounts(db_session):
    code = await seed(db_session)
    for uid in (82002, 82003):
        await register(db_session, uid, code.code)
    for exchange in ("gate", "bybit"):
        db_session.add(
            ExchangeAccount(
                user_id=82002,
                exchange=exchange,
                uid="own-uid",
                status="active",
                accrual_since=date(2026, 7, 10),
            )
        )
    await db_session.flush()
    counts = await service.stats(db_session, 82001)
    assert counts.invited == 2 and counts.with_accounts == 1
    assert (await service.stats(db_session, 82002)).invited == 0


async def test_all_changes_roll_back_with_registration(db_session):
    code = await seed(db_session)
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await register(db_session, 82002, code.code)
            raise RuntimeError("rollback")
    assert await db_session.get(User, 82002) is None
    assert await db_session.get(InviteAttribution, 82002) is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "invite_attributed")
        )
        == 0
    )


async def test_random_collision_is_retried(db_session, monkeypatch):
    await seed(db_session)
    occupied = (await service.current_code(db_session, 82001)).code
    candidate = next(c for c in ("a1b2c", "z9y8x") if c != occupied)
    sequence = iter((occupied, candidate))
    monkeypatch.setattr(service, "_new_code", lambda: next(sequence))
    code = await seed(db_session, 82002)
    assert code.code == candidate


async def test_generator_always_has_five_letters_and_digits():
    for _ in range(1000):
        assert service._is_short_code(service._new_code())
    assert not hasattr(service, "change_code")


async def test_upgrade_collision_keeps_old_code_when_retries_exhausted(db_session, monkeypatch):
    occupied = await seed(db_session, 82002)
    value = occupied.code
    db_session.add(User(id=82001, role="user", status="active"))
    await db_session.flush()
    old = InviteCode(user_id=82001, code="a" * 24)
    db_session.add(old)
    await db_session.flush()
    monkeypatch.setattr(service, "_new_code", lambda: value)
    with pytest.raises(service.InviteError, match="Не удалось"):
        await service.current_code(db_session, 82001)
    await db_session.refresh(old)
    assert old.is_current
    assert await db_session.scalar(select(func.count()).select_from(InviteCode)) == 2


async def test_concurrent_registration_and_generation_are_atomic(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    ids = [82991, 82992, 82993, 82994]
    async with sm() as session, session.begin():
        one = await seed(session, ids[0])
        two = await seed(session, ids[1])
        session.add(User(id=ids[3], role="user", status="active"))

    async def join(code):
        async with sm() as session, session.begin():
            return await register(session, ids[2], code)

    async def obtain(user_id):
        async with sm() as session, session.begin():
            return (await service.current_code(session, user_id)).code

    try:
        joined = await asyncio.gather(join(one.code), join(two.code))
        assert sum(r.outcome == "invited" for r in joined) == 1
        codes = await asyncio.gather(obtain(ids[3]), obtain(ids[3]))
        assert codes[0] == codes[1] and service._is_short_code(codes[0])
        async with sm() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(InviteCode).where(InviteCode.user_id == ids[3])
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(InviteAttribution)
                    .where(InviteAttribution.invitee_id == ids[2])
                )
                == 1
            )
    finally:
        async with sm() as session, session.begin():
            await session.execute(
                delete(InviteAttribution).where(InviteAttribution.invitee_id.in_(ids))
            )
            await session.execute(delete(InviteCode).where(InviteCode.user_id.in_(ids)))
            await session.execute(
                delete(AuditEvent).where(
                    AuditEvent.actor.in_([str(uid) for uid in ids]),
                    AuditEvent.entity_type == "invite",
                )
            )
            await session.execute(delete(User).where(User.id.in_(ids)))
