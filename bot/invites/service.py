"""Invitation code ownership and first-registration referral binding."""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui.repository import upsert_user
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.invites import InviteAttribution, InviteCode
from bot.db.models.ops import AuditEvent

PREFIX = "r_"
CODE_LENGTH = 5
_ALPHABET = string.ascii_lowercase + string.digits
_CODE = re.compile(r"^[a-z0-9_-]{5,32}$")
_RESERVED = frozenset({"admin", "support", "official", "moderator", "owner", "system", "referral"})


class InviteError(ValueError):
    """Safe user-facing validation or ownership error."""


class _CodeCollision(Exception):
    """Retry a generated name without retiring an existing working link."""


def _is_short_code(code: str) -> bool:
    return (
        len(code) == CODE_LENGTH
        and code.isascii()
        and code.isalnum()
        and code.islower()
        and any(c.isdigit() for c in code)
    )


def _new_code() -> str:
    for _ in range(64):
        value = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
        if _is_short_code(value):
            return value
    raise InviteError("Не удалось создать ссылку. Пожалуйста, попробуйте позже.")


@dataclass(frozen=True)
class Registration:
    created: bool
    outcome: Literal["none", "invited", "existing", "invalid", "self"]


@dataclass(frozen=True)
class InviteStats:
    invited: int
    with_accounts: int


def normalize_code(raw: str) -> str:
    raw = raw.strip()
    code = raw.lower()
    if not raw.isascii() or not _CODE.fullmatch(code):
        raise InviteError("Код должен содержать от 5 до 32 латинских букв, цифр, символов _ или -.")
    if code in _RESERVED:
        raise InviteError("Этот код зарезервирован. Пожалуйста, выберите другой.")
    return code


def payload_code(payload: str | None) -> str | None:
    if payload is None or not payload.startswith(PREFIX):
        return None
    try:
        return normalize_code(payload[len(PREFIX) :])
    except InviteError:
        return None


async def _lock_user(session: AsyncSession, user_id: int) -> User:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise InviteError("Сначала откройте личный кабинет через /start.")
    if user.status != "active":
        raise InviteError("Реферальная программа для этого аккаунта недоступна.")
    return user


def _audit(
    session: AsyncSession, user_id: int, action: str, entity_id: str, detail: dict[str, int | str]
) -> None:
    session.add(
        AuditEvent(
            actor=str(user_id),
            action=action,
            entity_type="invite",
            entity_id=entity_id,
            new_value=detail,
        )
    )


async def current_code(session: AsyncSession, user_id: int) -> InviteCode:
    await _lock_user(session, user_id)
    code = await session.scalar(
        select(InviteCode).where(InviteCode.user_id == user_id, InviteCode.is_current.is_(True))
    )
    if code is not None and _is_short_code(code.code):
        return code
    for _ in range(32):
        value = _new_code()
        try:
            async with session.begin_nested():
                if code is not None:
                    await session.execute(
                        update(InviteCode)
                        .where(InviteCode.user_id == user_id, InviteCode.is_current.is_(True))
                        .values(is_current=False)
                    )
                created = await session.scalar(
                    insert(InviteCode)
                    .values(user_id=user_id, code=value)
                    .on_conflict_do_nothing(index_elements=[InviteCode.code])
                    .returning(InviteCode)
                )
                if created is None:
                    raise _CodeCollision
        except _CodeCollision:
            continue
        action = "invite_code_upgraded" if code is not None else "invite_code_created"
        _audit(session, user_id, action, str(created.id), {"code": value})
        return created
    raise InviteError("Не удалось создать ссылку. Пожалуйста, попробуйте позже.")


async def register_start(
    session: AsyncSession, *, user_id: int, username: str | None, payload: str | None, now: datetime
) -> Registration:
    # INSERT ON CONFLICT distinguishes new users atomically, including concurrent /start.
    created_id = await session.scalar(
        insert(User)
        .values(id=user_id, role="user", status="active")
        .on_conflict_do_nothing(index_elements=[User.id])
        .returning(User.id)
    )
    await session.scalar(select(User).where(User.id == user_id).with_for_update())
    await upsert_user(session, user_id, username)
    requested = payload is not None and payload.startswith(PREFIX)
    code_value = payload_code(payload)
    if not requested:
        return Registration(created_id is not None, "none")
    if code_value is None:
        return Registration(created_id is not None, "invalid")
    code = await session.scalar(
        select(InviteCode)
        .join(User, User.id == InviteCode.user_id)
        .where(InviteCode.code == code_value, User.status == "active")
    )
    if code is None:
        return Registration(created_id is not None, "invalid")
    if code.user_id == user_id:
        return Registration(created_id is not None, "self")
    if created_id is None:
        return Registration(False, "existing")
    # Codes belong to pre-existing users; binding ONLY a newly inserted invitee
    # makes the referral graph acyclic without recursive lookups or reparenting.
    attribution = InviteAttribution(
        invitee_id=user_id, referrer_id=code.user_id, code_id=code.id, created_at=now
    )
    session.add(attribution)
    await session.flush()
    _audit(
        session,
        user_id,
        "invite_attributed",
        str(user_id),
        {"referrer_id": code.user_id, "code_id": code.id},
    )
    return Registration(True, "invited")


async def stats(session: AsyncSession, user_id: int) -> InviteStats:
    total = await session.scalar(
        select(func.count())
        .select_from(InviteAttribution)
        .where(InviteAttribution.referrer_id == user_id)
    )
    active = (
        select(ExchangeAccount.id)
        .where(
            ExchangeAccount.user_id == InviteAttribution.invitee_id,
            ExchangeAccount.status == "active",
        )
        .exists()
    )
    bound = await session.scalar(
        select(func.count())
        .select_from(InviteAttribution)
        .where(InviteAttribution.referrer_id == user_id, active)
    )
    return InviteStats(int(total or 0), int(bound or 0))
