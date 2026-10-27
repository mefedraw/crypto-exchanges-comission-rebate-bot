from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent, Notification
from bot.db.models.support import SupportCase, SupportMessage
from bot.notifications import poller
from bot.support import notifications, rendering, service

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = datetime(2026, 10, 27, 10, tzinfo=UTC)
ADMINS = frozenset({9001, 9002})
TOKEN = "a" * 24


async def seed(
    session, *, user_id=81001, gross="100", exchange="gate", state="paid", finalized=True
):
    session.add(User(id=user_id, username="user_name", role="user", status="active"))
    await session.flush()
    account = ExchangeAccount(
        user_id=user_id,
        exchange=exchange,
        uid=str(user_id),
        status="active",
        accrual_since=date(2026, 7, 10),
    )
    session.add(account)
    await session.flush()
    amount = Decimal(gross)
    session.add(
        RebateAccrual(
            user_id=user_id,
            account_id=account.id,
            accrual_date=date(2026, 10, 1),
            amount_usdt=amount,
            amount_native=amount,
            net_usdt=amount * Decimal(".9"),
            net_native=amount * Decimal(".9"),
            commission_usdt=amount * Decimal(".1"),
            effective_rank=1,
            fee_pct=Decimal(".1"),
            state=state,
            available_at=NOW,
            expires_at=NOW + timedelta(days=30),
            finalized=finalized,
        )
    )
    await session.flush()
    return account


async def opened(session, account, *, token=TOKEN):
    return await service.open_case(
        session,
        user_id=account.user_id,
        account_id=account.id,
        body="Не совпадают начисления <test> & UID",
        token=token,
        admin_ids=ADMINS,
        now=NOW,
    )


@pytest.mark.parametrize(
    "gross,allowed", [("99.999999999999", False), ("100", True), ("100.000000000001", True)]
)
async def test_exact_threshold_counts_paid_gross_not_current_net(db_session, gross, allowed):
    account = await seed(db_session, gross=gross)
    assert await service.lifetime_gross(db_session, account.user_id) == Decimal(gross)
    if allowed:
        receipt = await opened(db_session, account)
        assert receipt.created and receipt.case.gross_at_open == Decimal(gross)
    else:
        with pytest.raises(service.CaseError, match="100 USDT"):
            await opened(db_session, account)
        assert await db_session.scalar(select(func.count()).select_from(SupportCase)) == 0


async def test_finalized_only_and_sum_across_accounts(db_session):
    account = await seed(db_session, gross="60")
    second = ExchangeAccount(
        user_id=account.user_id,
        exchange="bitget",
        uid="other",
        status="active",
        accrual_since=date(2026, 7, 10),
    )
    db_session.add(second)
    await db_session.flush()
    db_session.add(
        RebateAccrual(
            user_id=account.user_id,
            account_id=second.id,
            accrual_date=date(2026, 10, 1),
            amount_usdt=Decimal("40"),
            commission_usdt=Decimal("4"),
            net_usdt=Decimal("36"),
            effective_rank=1,
            fee_pct=Decimal(".1"),
            state="expired",
            available_at=NOW,
            expires_at=NOW + timedelta(days=30),
            finalized=True,
        )
    )
    await db_session.flush()
    assert await service.lifetime_gross(db_session, account.user_id) == Decimal("100")
    assert (await opened(db_session, account)).created


async def test_unfinalized_amounts_do_not_grant_access(db_session):
    account = await seed(db_session, gross="200", finalized=False)
    assert await service.lifetime_gross(db_session, account.user_id) == 0
    with pytest.raises(service.CaseError):
        await opened(db_session, account)


async def test_uid_ownership_and_active_status_checked_at_submission(db_session):
    account = await seed(db_session)
    other = await seed(db_session, user_id=81002)
    with pytest.raises(service.CaseError, match="UID"):
        await service.open_case(
            db_session,
            user_id=account.user_id,
            account_id=other.id,
            body="test",
            token=TOKEN,
            admin_ids=ADMINS,
            now=NOW,
        )
    account.status = "archived"
    await db_session.flush()
    with pytest.raises(service.CaseError, match="UID"):
        await opened(db_session, account)


async def test_repeated_submission_is_one_case_message_audit_and_outbox(db_session):
    account = await seed(db_session)
    first = await opened(db_session, account)
    second = await opened(db_session, account)
    assert first.created and not second.created and first.case.id == second.case.id
    assert await db_session.scalar(select(func.count()).select_from(SupportCase)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SupportMessage)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AuditEvent)) == 1
    outbox = (await db_session.scalars(select(Notification))).all()
    assert {n.recipient_chat_id for n in outbox} == ADMINS
    assert all(
        n.kind == service.OPENED and set(n.payload) == {"case_id", "message_id"} for n in outbox
    )
    with pytest.raises(service.CaseError, match="устарел"):
        await service.open_case(
            db_session,
            user_id=account.user_id,
            account_id=account.id,
            body="different",
            token=TOKEN,
            admin_ids=ADMINS,
            now=NOW,
        )


async def test_another_open_case_same_uid_is_rejected_and_closed_can_be_reopened(db_session):
    account = await seed(db_session)
    first = await opened(db_session, account)
    with pytest.raises(service.CaseError, match="уже открыт"):
        await opened(db_session, account, token="b" * 24)
    await service.close_case(
        db_session, case_id=first.case.id, actor_id=9001, admin_ids=ADMINS, now=NOW
    )
    assert (await opened(db_session, account, token="b" * 24)).created


async def test_case_outbox_and_audit_rollback_together(db_session):
    account = await seed(db_session)
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await opened(db_session, account)
            raise RuntimeError("rollback")
    for model in (SupportCase, SupportMessage, Notification, AuditEvent):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0


async def test_unrelated_user_cannot_read_reply_or_close(db_session):
    account = await seed(db_session)
    receipt = await opened(db_session, account)
    for action in ("view", "reply", "close"):
        with pytest.raises(service.CaseError, match="недоступен"):
            if action == "view":
                await service.get_case(db_session, receipt.case.id, 666, ADMINS)
            elif action == "reply":
                await service.reply(
                    db_session,
                    case_id=receipt.case.id,
                    actor_id=666,
                    body="unauthorized",
                    token="b" * 24,
                    admin_ids=ADMINS,
                    now=NOW,
                )
            else:
                await service.close_case(
                    db_session, case_id=receipt.case.id, actor_id=666, admin_ids=ADMINS, now=NOW
                )


async def test_staff_reply_then_user_followup_is_persistent_and_idempotent(db_session):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    for expected in (True, False):
        result = await service.reply(
            db_session,
            case_id=case.id,
            actor_id=9001,
            body="Проверим <текст>",
            token="b" * 24,
            admin_ids=ADMINS,
            now=NOW,
        )
        assert result.created == expected
    with pytest.raises(service.CaseError, match="минуту"):
        await service.reply(
            db_session,
            case_id=case.id,
            actor_id=account.user_id,
            body="еще",
            token="c" * 24,
            admin_ids=ADMINS,
            now=NOW,
        )
    await service.reply(
        db_session,
        case_id=case.id,
        actor_id=account.user_id,
        body="Подробности",
        token="c" * 24,
        admin_ids=ADMINS,
        now=NOW + timedelta(minutes=1),
    )
    items = await service.messages(db_session, case.id)
    assert [m.is_staff for m in items] == [False, True, False]
    assert (
        await service.close_case(
            db_session, case_id=case.id, actor_id=9001, admin_ids=ADMINS, now=NOW
        )
    ).created
    assert not (
        await service.close_case(
            db_session, case_id=case.id, actor_id=9001, admin_ids=ADMINS, now=NOW
        )
    ).created
    with pytest.raises(service.CaseError, match="закрыт"):
        await service.reply(
            db_session,
            case_id=case.id,
            actor_id=9001,
            body="later",
            token="d" * 24,
            admin_ids=ADMINS,
            now=NOW,
        )


@pytest.mark.parametrize("body", [None, "", "  \n", "x" * 1201, "bad\x00text"])
async def test_invalid_text_rejected(body):
    with pytest.raises(service.CaseError):
        service.validate_body(body)


async def test_rendering_escapes_user_content_and_preview_matches_notification(
    db_session, monkeypatch
):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    monkeypatch.setattr(
        notifications, "get_settings", lambda: SimpleNamespace(admin_telegram_ids=ADMINS)
    )
    note = await db_session.scalar(select(Notification).order_by(Notification.id))
    text, keyboard = await notifications.render(db_session, note)
    assert "&lt;test&gt; &amp; UID" in text and "<test>" not in text
    assert text == rendering.case_request(case, "Не совпадают начисления <test> & UID")
    assert any(b.text == "Ответить" for row in keyboard.inline_keyboard for b in row)
    note.recipient_chat_id = 777
    assert await notifications.render(db_session, note) is None


async def test_floor_display_does_not_show_100_for_ineligible_user():
    assert rendering.amount(Decimal("99.999")) == "99.99"


async def test_daily_limit_counts_closed_cases(db_session):
    account = await seed(db_session)
    for i in range(2):
        case = (await opened(db_session, account, token=f"{i:024x}")).case
        await service.close_case(
            db_session, case_id=case.id, actor_id=account.user_id, admin_ids=ADMINS, now=NOW
        )
    with pytest.raises(service.CaseError, match="24 часа.*2 обращения"):
        await opened(db_session, account, token="f" * 24)


async def test_daily_limit_counts_all_uids_and_repeated_confirmation_is_free(db_session):
    account = await seed(db_session)
    second = ExchangeAccount(
        user_id=account.user_id,
        exchange="bitget",
        uid="81001-other",
        status="active",
        accrual_since=date(2026, 7, 10),
    )
    third = ExchangeAccount(
        user_id=account.user_id,
        exchange="kucoin",
        uid="81001-third",
        status="active",
        accrual_since=date(2026, 7, 10),
    )
    db_session.add_all([second, third])
    await db_session.flush()
    first_receipt = await opened(db_session, account)
    await opened(db_session, second, token="b" * 24)
    replay = await opened(db_session, account)
    assert not replay.created and replay.case.id == first_receipt.case.id
    with pytest.raises(service.CaseError, match="24 часа.*2 обращения"):
        await opened(db_session, third, token="c" * 24)
    assert await db_session.scalar(select(func.count()).select_from(SupportCase)) == 2
    assert await db_session.scalar(select(func.count()).select_from(SupportMessage)) == 2
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 4


async def test_daily_limit_has_an_exact_rolling_24_hour_boundary(db_session):
    account = await seed(db_session)
    for i in range(2):
        case = (await opened(db_session, account, token=f"{i:024x}")).case
        await service.close_case(
            db_session, case_id=case.id, actor_id=account.user_id, admin_ids=ADMINS, now=NOW
        )
    args = {
        "user_id": account.user_id,
        "account_id": account.id,
        "body": "Next day",
        "token": "e" * 24,
        "admin_ids": ADMINS,
    }
    with pytest.raises(service.CaseError, match="24 часа"):
        await service.open_case(db_session, **args, now=NOW + timedelta(days=1, microseconds=-1))
    assert (await service.open_case(db_session, **args, now=NOW + timedelta(days=1))).created


async def test_daily_limit_does_not_limit_other_users_or_existing_replies(db_session):
    account = await seed(db_session)
    first = await opened(db_session, account)
    await service.close_case(
        db_session, case_id=first.case.id, actor_id=account.user_id, admin_ids=ADMINS, now=NOW
    )
    current = await opened(db_session, account, token="b" * 24)
    assert (
        await service.reply(
            db_session,
            case_id=current.case.id,
            actor_id=account.user_id,
            body="Follow-up",
            token="c" * 24,
            admin_ids=ADMINS,
            now=NOW + timedelta(minutes=1),
        )
    ).created
    other = await seed(db_session, user_id=81002)
    assert (await opened(db_session, other, token="d" * 24)).created


async def test_open_case_limit_remains_separate_from_daily_limit(db_session):
    account = await seed(db_session)
    for i in range(3):
        current = account
        if i:
            current = ExchangeAccount(
                user_id=account.user_id,
                exchange="gate",
                uid=f"old-case-{i}",
                status="active",
                accrual_since=date(2026, 7, 10),
            )
            db_session.add(current)
            await db_session.flush()
        db_session.add(
            SupportCase(
                user_id=account.user_id,
                account_id=current.id,
                exchange=current.exchange,
                uid=current.uid,
                gross_at_open=Decimal("100"),
                request_token=f"{i:024x}",
                created_at=NOW - timedelta(days=2),
                updated_at=NOW - timedelta(days=2),
            )
        )
    await db_session.flush()
    with pytest.raises(service.CaseError, match="3 открытых обращения"):
        await service.check_creation_limits(db_session, account.user_id, NOW)


async def test_delivery_retries_with_buttons_without_duplicate_case(db_session, monkeypatch):
    db_session.sync_session.expire_on_commit = False
    account = await seed(db_session)
    await opened(db_session, account)
    monkeypatch.setattr(
        notifications, "get_settings", lambda: SimpleNamespace(admin_telegram_ids=ADMINS)
    )

    @asynccontextmanager
    async def sessions():
        yield db_session

    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("temporary delivery error")
    assert await poller.deliver_pending(bot, sessions, chain_id=56, now=lambda: NOW) == 0
    bot.send_message.side_effect = None
    assert await poller.deliver_pending(bot, sessions, chain_id=56, now=lambda: NOW) == 2
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None
    assert await poller.deliver_pending(bot, sessions, chain_id=56, now=lambda: NOW) == 0
    assert await db_session.scalar(select(func.count()).select_from(SupportCase)) == 1


async def test_concurrent_confirmations_only_create_once(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as session:
        account = await seed(session, user_id=81099)
        user_id, account_id = account.user_id, account.id
        await session.commit()

    async def submit():
        async with sm() as session, session.begin():
            return await service.open_case(
                session,
                user_id=user_id,
                account_id=account_id,
                body="Concurrent",
                token="e" * 24,
                admin_ids=ADMINS,
                now=NOW,
            )

    try:
        receipts = await asyncio.gather(submit(), submit())
        assert sorted(r.created for r in receipts) == [False, True]
        assert receipts[0].case.id == receipts[1].case.id
    finally:
        async with sm() as session, session.begin():
            case_ids = select(SupportCase.id).where(SupportCase.user_id == user_id)
            ids = list((await session.scalars(case_ids)).all())
            for case_id in ids:
                await session.execute(
                    delete(Notification).where(
                        Notification.payload["case_id"].astext == str(case_id)
                    )
                )
                await session.execute(
                    delete(AuditEvent).where(
                        AuditEvent.entity_type == "support_case",
                        AuditEvent.entity_id == str(case_id),
                    )
                )
            await session.execute(
                delete(SupportMessage).where(SupportMessage.case_id.in_(case_ids))
            )
            await session.execute(delete(SupportCase).where(SupportCase.user_id == user_id))
            await session.execute(
                delete(RebateAccrual).where(RebateAccrual.account_id == account_id)
            )
            await session.execute(delete(ExchangeAccount).where(ExchangeAccount.id == account_id))
            await session.execute(delete(User).where(User.id == user_id))
