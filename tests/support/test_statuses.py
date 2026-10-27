from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from bot.admin.keyboards import admin_menu
from bot.db.models.ops import AuditEvent, Notification
from bot.db.models.support import SupportCase
from bot.support import keyboards, notifications, service, statuses
from tests.support.test_service import ADMINS, NOW, opened, seed

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def owner_notes(session, user_id):
    return list(
        (
            await session.scalars(
                select(Notification)
                .where(Notification.recipient_chat_id == user_id)
                .order_by(Notification.id)
            )
        ).all()
    )


async def staff_reply(session, case, *, token="b" * 24):
    return await service.reply(
        session,
        case_id=case.id,
        actor_id=9001,
        body="Пришлите выписку <safe>",
        token=token,
        admin_ids=ADMINS,
        now=NOW,
    )


async def test_complete_status_lifecycle_and_only_two_owner_notification_kinds(db_session):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    assert case.status == statuses.SENT
    assert await owner_notes(db_session, account.user_id) == []
    args = {"case_id": case.id, "actor_id": 9001, "admin_ids": ADMINS, "now": NOW}
    assert (await service.take_case(db_session, **args)).created
    assert case.status == statuses.OPEN
    assert not (await service.take_case(db_session, **args)).created
    assert await owner_notes(db_session, account.user_id) == []
    await staff_reply(db_session, case)
    assert case.status == statuses.REQUIRES_REPLY
    assert not (await service.take_case(db_session, **args)).created
    assert case.status == statuses.REQUIRES_REPLY
    await service.reply(
        db_session,
        case_id=case.id,
        actor_id=account.user_id,
        body="Вот выписка",
        token="c" * 24,
        admin_ids=ADMINS,
        now=NOW + timedelta(minutes=1),
    )
    assert case.status == statuses.OPEN
    assert (await service.close_case(db_session, **args)).created
    assert case.status == statuses.CLOSED and case.closed_at == NOW
    assert not (await service.close_case(db_session, **args)).created
    notes = await owner_notes(db_session, account.user_id)
    assert [n.kind for n in notes] == [service.ACTION_REQUIRED, service.CLOSED]
    with pytest.raises(service.CaseError, match="закрыт"):
        await service.take_case(db_session, **args)
    with pytest.raises(service.CaseError, match="закрыт"):
        await staff_reply(db_session, case, token="d" * 24)


async def test_owner_followup_does_not_take_the_case_on_behalf_of_support(db_session):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    await service.reply(
        db_session,
        case_id=case.id,
        actor_id=account.user_id,
        body="Еще подробности",
        token="b" * 24,
        admin_ids=ADMINS,
        now=NOW + timedelta(minutes=1),
    )
    assert case.status == statuses.SENT
    for actor in (account.user_id, 777):
        with pytest.raises(service.CaseError, match="администратор"):
            await service.take_case(
                db_session, case_id=case.id, actor_id=actor, admin_ids=ADMINS, now=NOW
            )


async def test_status_audit_and_notification_roll_back_with_the_message(db_session):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await staff_reply(db_session, case)
            raise RuntimeError("rollback")
    await db_session.refresh(case)
    assert case.status == statuses.SENT
    assert await owner_notes(db_session, account.user_id) == []
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "support_status_changed")
        )
        == 0
    )


async def test_notifications_are_short_private_and_skip_answered_or_closed_requests(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        notifications, "get_settings", lambda: SimpleNamespace(admin_telegram_ids=ADMINS)
    )
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    await staff_reply(db_session, case)
    note = (await owner_notes(db_session, account.user_id))[0]
    text, markup = await notifications.render(db_session, note)
    assert "требуется ваш ответ" in text and "Пришлите" not in text
    assert [b.text for row in markup.inline_keyboard for b in row] == ["Открыть кейс"]
    assert markup.inline_keyboard[0][0].callback_data == f"case:view:{case.id}:-"
    legacy = Notification(
        recipient_chat_id=account.user_id, kind=service.REPLIED, payload=note.payload
    )
    assert await notifications.render(db_session, legacy) == (text, markup)
    spoofed = Notification(
        recipient_chat_id=777, kind=service.ACTION_REQUIRED, payload=note.payload
    )
    assert await notifications.render(db_session, spoofed) is None
    sent = Notification(
        recipient_chat_id=account.user_id, kind=service.OPENED, payload=note.payload
    )
    assert await notifications.render(db_session, sent) is None
    await service.reply(
        db_session,
        case_id=case.id,
        actor_id=account.user_id,
        body="Ответ",
        token="c" * 24,
        admin_ids=ADMINS,
        now=NOW + timedelta(minutes=1),
    )
    assert await notifications.render(db_session, note) is None
    await staff_reply(db_session, case, token="d" * 24)
    new_note = (await owner_notes(db_session, account.user_id))[-1]
    assert await notifications.render(db_session, note) is None
    assert await notifications.render(db_session, new_note) is not None
    await service.close_case(db_session, case_id=case.id, actor_id=9001, admin_ids=ADMINS, now=NOW)
    await db_session.flush()
    assert await notifications.render(db_session, new_note) is None
    closed = (await owner_notes(db_session, account.user_id))[-1]
    text, markup = await notifications.render(db_session, closed)
    assert "кейс закрыт" in text and "ознакомьтесь" in text and "Пришлите" not in text
    assert len(markup.inline_keyboard) == 1


async def test_owner_closing_receives_one_closed_notice_and_no_reply_is_possible(db_session):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    args = {"case_id": case.id, "actor_id": account.user_id, "admin_ids": ADMINS, "now": NOW}
    await service.close_case(db_session, **args)
    await service.close_case(db_session, **args)
    assert [n.kind for n in await owner_notes(db_session, account.user_id)] == [service.CLOSED]
    buttons = [b.text for row in keyboards.case_actions(case).inline_keyboard for b in row]
    assert "Ответить" not in buttons and "Дополнить" not in buttons


@pytest.mark.parametrize("bad_status", ["unknown", "closed"])
async def test_database_rejects_unknown_status_and_inconsistent_closure(db_session, bad_status):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(SupportCase).where(SupportCase.id == case.id).values(status=bad_status)
            )


async def test_admin_labels_lists_and_user_actions_show_all_four_statuses():
    buttons = [b.text for row in admin_menu().inline_keyboard for b in row]
    assert "Обращения" in buttons and "Кейсы" not in buttons
    for status, label in statuses.LABELS.items():
        case = SupportCase(
            id=42,
            user_id=1,
            exchange="gate",
            uid="12345678",
            status=status,
            closed_at=NOW if status == statuses.CLOSED else None,
        )
        markup = keyboards.case_list([case], 0, staff=True, closed=case.closed_at is not None)
        assert any(label in b.text for row in markup.inline_keyboard for b in row)
        assert len(markup.inline_keyboard[0]) == 2
        admin_actions = [
            b.text for row in keyboards.case_actions(case, staff=True).inline_keyboard for b in row
        ]
        assert ("Взять в работу" in admin_actions) == (status == statuses.SENT)
        if status == statuses.REQUIRES_REPLY:
            assert "Ответить" in [
                b.text for row in keyboards.case_actions(case).inline_keyboard for b in row
            ]


async def test_closed_admin_filter_does_not_hide_closed_cases_from_their_owner(db_session):
    account = await seed(db_session)
    case = (await opened(db_session, account)).case
    await service.close_case(db_session, case_id=case.id, actor_id=9001, admin_ids=ADMINS, now=NOW)
    await db_session.flush()
    assert await service.list_cases(db_session, 9001, staff=True) == []
    assert [c.id for c in await service.list_cases(db_session, 9001, staff=True, closed=True)] == [
        case.id
    ]
    assert [c.id for c in await service.list_cases(db_session, account.user_id)] == [case.id]
    assert await service.list_cases(db_session, 777) == []
