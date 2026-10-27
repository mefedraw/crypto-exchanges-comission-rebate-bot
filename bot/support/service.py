"""Support authorization, eligibility, durable messages, and delivery outbox."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent
from bot.db.models.support import SupportCase, SupportMessage
from bot.i18n import gettext as tr
from bot.notifications.repository import enqueue
from bot.support import statuses

MIN_GROSS = Decimal("100")
MAX_BODY = 1200
MAX_CASES_PER_DAY = 2
MAX_OPEN_CASES = 3
PAGE_SIZE = 10
OPENED = "support_opened"
REPLIED = "support_replied"
CLOSED = "support_closed"
ACTION_REQUIRED = "support_action_required"
KINDS = frozenset({OPENED, REPLIED, CLOSED, ACTION_REQUIRED})
_TOKEN_RE = re.compile(r"^[0-9a-f]{24}$")


class CaseError(ValueError):
    """A safe message that can be shown to the requester."""


@dataclass(frozen=True)
class CaseReceipt:
    case: SupportCase
    created: bool


def validate_body(raw: str | None) -> str:
    body = (raw or "").strip()
    if not body or len(body) > MAX_BODY or any(ord(c) < 32 and c not in "\n\t" for c in body):
        raise CaseError(
            tr("Пожалуйста, отправьте текст от 1 до {v0} символов.").format(v0=MAX_BODY)
        )
    return body


def _validate_token(token: str) -> None:
    if not _TOKEN_RE.fullmatch(token):
        raise CaseError(tr("Черновик устарел. Откройте кейс заново."))


async def lifetime_gross(session: AsyncSession, user_id: int) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(func.sum(RebateAccrual.amount_usdt), 0)).where(
            RebateAccrual.user_id == user_id, RebateAccrual.finalized.is_(True)
        )
    )
    return Decimal(value or 0)


async def accounts(session: AsyncSession, user_id: int) -> list[ExchangeAccount]:
    return list(
        (
            await session.scalars(
                select(ExchangeAccount)
                .where(ExchangeAccount.user_id == user_id, ExchangeAccount.status == "active")
                .order_by(ExchangeAccount.exchange, ExchangeAccount.id)
            )
        ).all()
    )


async def owned_account(session: AsyncSession, user_id: int, account_id: int) -> ExchangeAccount:
    account = await session.scalar(
        select(ExchangeAccount).where(
            ExchangeAccount.id == account_id,
            ExchangeAccount.user_id == user_id,
            ExchangeAccount.status == "active",
        )
    )
    if account is None:
        raise CaseError(tr("Этот UID больше не привязан к вашему аккаунту. Выберите другой."))
    return account


async def get_case(
    session: AsyncSession,
    case_id: int,
    actor_id: int,
    admin_ids: frozenset[int],
    *,
    lock: bool = False,
) -> SupportCase:
    statement = select(SupportCase).where(SupportCase.id == case_id)
    if actor_id not in admin_ids:
        statement = statement.where(SupportCase.user_id == actor_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    case = await session.scalar(statement)
    if case is None:
        raise CaseError(tr("Кейс не найден или недоступен."))
    return case


async def list_cases(
    session: AsyncSession,
    actor_id: int,
    *,
    staff: bool = False,
    page: int = 0,
    closed: bool = False,
) -> list[SupportCase]:
    statement = select(SupportCase)
    if staff:
        statement = statement.where(
            SupportCase.closed_at.is_not(None) if closed else SupportCase.closed_at.is_(None)
        )
    else:
        statement = statement.where(SupportCase.user_id == actor_id)
    return list(
        (
            await session.scalars(
                statement.order_by(SupportCase.updated_at.desc(), SupportCase.id.desc())
                .offset(max(0, page) * PAGE_SIZE)
                .limit(PAGE_SIZE + 1)
            )
        ).all()
    )


async def messages(session: AsyncSession, case_id: int) -> list[SupportMessage]:
    # Bound each UI page without loading an unbounded conversation into memory.
    return list(
        reversed(
            (
                await session.scalars(
                    select(SupportMessage)
                    .where(SupportMessage.case_id == case_id)
                    .order_by(SupportMessage.id.desc())
                    .limit(20)
                )
            ).all()
        )
    )


async def _notify(
    session: AsyncSession,
    kind: str,
    case: SupportCase,
    recipients: frozenset[int],
    message_id: int | None = None,
) -> None:
    for recipient in sorted(recipients):
        await enqueue(
            session,
            recipient_chat_id=recipient,
            kind=kind,
            payload={"case_id": case.id, "message_id": message_id},
        )


def _audit(session: AsyncSession, case: SupportCase, actor_id: int, action: str) -> None:
    session.add(
        AuditEvent(
            actor=str(actor_id), action=action, entity_type="support_case", entity_id=str(case.id)
        )
    )


def _set_status(session: AsyncSession, case: SupportCase, status: str, actor_id: int) -> None:
    previous = case.status
    if previous == status:
        return
    case.status = status
    session.add(
        AuditEvent(
            actor=str(actor_id),
            action="support_status_changed",
            entity_type="support_case",
            entity_id=str(case.id),
            old_value={"status": previous},
            new_value={"status": status},
        )
    )


async def take_case(
    session: AsyncSession, *, case_id: int, actor_id: int, admin_ids: frozenset[int], now: datetime
) -> CaseReceipt:
    if actor_id not in admin_ids:
        raise CaseError(tr("Взять обращение в работу может только администратор."))
    case = await get_case(session, case_id, actor_id, admin_ids, lock=True)
    if case.closed_at is not None:
        raise CaseError(tr("Этот кейс уже закрыт."))
    if case.status != statuses.SENT:
        return CaseReceipt(case, False)
    _set_status(session, case, statuses.OPEN, actor_id)
    case.updated_at = now
    await session.flush()
    return CaseReceipt(case, True)


async def check_creation_limits(session: AsyncSession, user_id: int, now: datetime) -> None:
    recent = await session.scalar(
        select(func.count())
        .select_from(SupportCase)
        .where(SupportCase.user_id == user_id, SupportCase.created_at > now - timedelta(days=1))
    )
    if (recent or 0) >= MAX_CASES_PER_DAY:
        raise CaseError(
            tr(
                "За последние 24 часа вы уже создали 2 обращения. Пожалуйста, "
                "попробуйте позже или дополните существующее в разделе «Мои "
                "обращения»."
            )
        )
    open_count = await session.scalar(
        select(func.count())
        .select_from(SupportCase)
        .where(SupportCase.user_id == user_id, SupportCase.closed_at.is_(None))
    )
    if (open_count or 0) >= MAX_OPEN_CASES:
        raise CaseError(
            tr(
                "У вас уже 3 открытых обращения. Пожалуйста, дополните "
                "существующее в разделе «Мои обращения» или дождитесь его решения."
            )
        )


async def open_case(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    body: str,
    token: str,
    admin_ids: frozenset[int],
    now: datetime,
) -> CaseReceipt:
    body = validate_body(body)
    _validate_token(token)
    if not admin_ids:
        raise CaseError(tr("Обращения временно недоступны. Пожалуйста, попробуйте позже."))
    # One user lock serializes duplicate submissions and cross-UID creation limits.
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or user.status != "active":
        raise CaseError(tr("Обращения для этого аккаунта недоступны."))
    prior = await session.scalar(select(SupportCase).where(SupportCase.request_token == token))
    if prior is not None:
        first = await session.scalar(
            select(SupportMessage)
            .where(SupportMessage.case_id == prior.id)
            .order_by(SupportMessage.id)
            .limit(1)
        )
        if (
            prior.user_id != user_id
            or prior.account_id != account_id
            or first is None
            or first.body != body
        ):
            raise CaseError(tr("Черновик устарел. Откройте кейс заново."))
        return CaseReceipt(prior, False)
    account = await session.scalar(
        select(ExchangeAccount)
        .where(
            ExchangeAccount.id == account_id,
            ExchangeAccount.user_id == user_id,
            ExchangeAccount.status == "active",
        )
        .with_for_update()
    )
    if account is None:
        raise CaseError(tr("Этот UID больше не привязан к вашему аккаунту. Выберите другой."))
    gross = await lifetime_gross(session, user_id)
    if gross < MIN_GROSS:
        raise CaseError(
            tr("Для открытия кейса нужно накопить не менее 100 USDT рефбека за все время.")
        )
    existing = await session.scalar(
        select(SupportCase).where(
            SupportCase.user_id == user_id,
            SupportCase.account_id == account_id,
            SupportCase.closed_at.is_(None),
        )
    )
    if existing is not None:
        raise CaseError(
            tr(
                "По этому UID уже открыт кейс №{v0}. Дополните его в разделе «Мои " "обращения»."
            ).format(v0=existing.id)
        )
    await check_creation_limits(session, user_id, now)
    case = SupportCase(
        user_id=user_id,
        account_id=account.id,
        exchange=account.exchange,
        uid=account.uid,
        username=user.username,
        gross_at_open=gross,
        request_token=token,
        status=statuses.SENT,
        created_at=now,
        updated_at=now,
    )
    session.add(case)
    await session.flush()
    first = SupportMessage(
        case_id=case.id,
        author_id=user_id,
        is_staff=False,
        body=body,
        request_token=token,
        created_at=now,
    )
    session.add(first)
    await session.flush()
    await _notify(session, OPENED, case, admin_ids - {case.user_id}, first.id)
    _audit(session, case, user_id, "support_case_opened")
    return CaseReceipt(case, True)


async def reply(
    session: AsyncSession,
    *,
    case_id: int,
    actor_id: int,
    body: str,
    token: str,
    admin_ids: frozenset[int],
    now: datetime,
) -> CaseReceipt:
    body = validate_body(body)
    _validate_token(token)
    case = await get_case(session, case_id, actor_id, admin_ids, lock=True)
    staff = actor_id in admin_ids
    prior = await session.scalar(
        select(SupportMessage).where(SupportMessage.request_token == token)
    )
    if prior is not None:
        if (prior.case_id, prior.author_id, prior.body) != (case_id, actor_id, body):
            raise CaseError(tr("Черновик устарел. Откройте кейс заново."))
        return CaseReceipt(case, False)
    if case.closed_at is not None:
        raise CaseError(tr("Этот кейс уже закрыт."))
    if not staff:
        last_user = await session.scalar(
            select(func.max(SupportMessage.created_at)).where(
                SupportMessage.case_id == case.id, SupportMessage.is_staff.is_(False)
            )
        )
        if last_user is not None and last_user > now - timedelta(seconds=60):
            raise CaseError(tr("Пожалуйста, подождите минуту перед следующим сообщением в кейсе."))
    message = SupportMessage(
        case_id=case.id,
        author_id=actor_id,
        is_staff=staff,
        body=body,
        request_token=token,
        created_at=now,
    )
    session.add(message)
    case.updated_at = now
    target = (
        statuses.REQUIRES_REPLY
        if staff
        else (statuses.SENT if case.status == statuses.SENT else statuses.OPEN)
    )
    _set_status(session, case, target, actor_id)
    await session.flush()
    recipients = frozenset({case.user_id}) if staff else admin_ids - {case.user_id}
    await _notify(session, ACTION_REQUIRED if staff else REPLIED, case, recipients, message.id)
    _audit(session, case, actor_id, "support_case_replied")
    return CaseReceipt(case, True)


async def close_case(
    session: AsyncSession, *, case_id: int, actor_id: int, admin_ids: frozenset[int], now: datetime
) -> CaseReceipt:
    case = await get_case(session, case_id, actor_id, admin_ids, lock=True)
    if case.closed_at is not None:
        return CaseReceipt(case, False)
    case.closed_at = case.updated_at = now
    case.closed_by = actor_id
    _set_status(session, case, statuses.CLOSED, actor_id)
    recipients = frozenset({case.user_id}) if actor_id in admin_ids else admin_ids | {case.user_id}
    await _notify(session, CLOSED, case, recipients)
    _audit(session, case, actor_id, "support_case_closed")
    return CaseReceipt(case, True)
