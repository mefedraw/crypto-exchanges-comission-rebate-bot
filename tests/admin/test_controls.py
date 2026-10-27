"""Task 5: personal commission rate (B), UID dispute resolution, user block/unblock."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin import repository as admin_repo
from bot.admin import service as admin_service
from bot.admin.router import (
    receive_block_uid,
    receive_rate,
    receive_reassign,
    receive_user_status,
    request_block_uid,
    request_rate,
    request_reassign,
    request_user_status,
    show_stats,
    show_user_controls,
    show_wallet_balance,
)
from bot.admin.states import AdminFlow
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent
from bot.payout.rank_timing import month_start
from bot.payout.ranks import fee_pct as rank_fee_pct

_async = pytest.mark.asyncio(loop_scope="session")


async def _user(
    session: AsyncSession, user_id: int, *, status: str = "active", username: str | None = None
) -> User:
    user = User(id=user_id, role="user", status=status, username=username)
    session.add(user)
    await session.flush()
    return user


async def _account(
    session: AsyncSession,
    user_id: int,
    exchange: str,
    uid: str,
    *,
    status: str = "active",
) -> ExchangeAccount:
    acc = ExchangeAccount(
        user_id=user_id,
        exchange=exchange,
        uid=uid,
        status=status,
        accrual_since=date(2026, 7, 1),
    )
    session.add(acc)
    await session.flush()
    return acc


async def _accrual(
    session: AsyncSession,
    user_id: int,
    account_id: int,
    *,
    accrual_date: date,
    state: str,
    effective_rank: int,
    fee_pct: Decimal,
    amount_usdt: Decimal = Decimal("100"),
    asset: str = "USDT",
    amount_native: Decimal = Decimal("0"),
) -> RebateAccrual:
    commission = amount_usdt * fee_pct
    net_usdt = amount_usdt - commission
    net_native = amount_native * (Decimal(1) - fee_pct)
    stamp = datetime.combine(accrual_date, datetime.min.time(), tzinfo=UTC)
    row = RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=accrual_date,
        asset=asset,
        amount_usdt=amount_usdt,
        effective_rank=effective_rank,
        fee_pct=fee_pct,
        commission_usdt=commission,
        net_usdt=net_usdt,
        amount_native=amount_native,
        net_native=net_native,
        state=state,
        available_at=stamp,
        expires_at=stamp + timedelta(days=30),
        raw_records_count=1,
    )
    session.add(row)
    await session.flush()
    return row


def _message() -> Message:
    # Real (frozen) Message so isinstance + panel.remember(msg.chat.id) work; async
    # methods are mocked via object.__setattr__ (the model is frozen).
    msg = Message.model_construct(message_id=1, chat=Chat.model_construct(id=1, type="private"))
    for _name in ("answer", "edit_text", "delete"):
        object.__setattr__(msg, _name, AsyncMock())
    return msg


def _callback(admin_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        message=_message(), from_user=SimpleNamespace(id=admin_id), answer=AsyncMock()
    )


def _ctx(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


# ---------------------------------------------------------------------------
# 📊 Статистика — the admin overview button (previously a dead callback)
# ---------------------------------------------------------------------------


@_async
async def test_show_stats_renders_overview(db_session: AsyncSession) -> None:
    await _user(db_session, 9801)
    await _user(db_session, 9802, status="blocked")
    await _account(db_session, 9801, "gate", "G9801")
    cb = _callback(9801)
    await show_stats(cb, _ctx(9999), db_session)  # type: ignore[arg-type]
    cb.answer.assert_awaited()  # spinner is dismissed (the old dead button never did)
    cb.message.edit_text.assert_awaited()
    text = cb.message.edit_text.await_args.args[0]
    assert "Статистика" in text
    assert "Профит" in text  # earnings block present
    assert "Заработано (брутто)" in text
    assert "Рефбек юзерам" in text
    assert "Пользователи: 2" in text
    assert "заблокировано: 1" in text
    assert "Активных UID: 1" in text


@_async
async def test_admin_overview_earned_rebate_profit(db_session: AsyncSession) -> None:
    """earned = Σ gross, rebate = Σ net, profit = Σ commission — total and current month."""
    await _user(db_session, 9810)
    acc = await _account(db_session, 9810, "gate", "G9810")
    # July row (current month for the test's frozen "today") + a June row.
    db_session.add(
        RebateAccrual(
            user_id=9810,
            account_id=acc.id,
            accrual_date=date(2026, 7, 5),
            amount_usdt=Decimal("100"),
            effective_rank=2,
            fee_pct=Decimal("0.07"),
            commission_usdt=Decimal("7"),
            net_usdt=Decimal("93"),
            state="available",
            available_at=datetime(2026, 7, 6, tzinfo=UTC),
            expires_at=datetime(2026, 8, 4, tzinfo=UTC),
            raw_records_count=1,
            adapter_notes=None,
        )
    )
    db_session.add(
        RebateAccrual(
            user_id=9810,
            account_id=acc.id,
            accrual_date=date(2026, 6, 10),
            amount_usdt=Decimal("50"),
            effective_rank=1,
            fee_pct=Decimal("0.10"),
            commission_usdt=Decimal("5"),
            net_usdt=Decimal("45"),
            state="paid",
            available_at=datetime(2026, 6, 11, tzinfo=UTC),
            expires_at=datetime(2026, 7, 10, tzinfo=UTC),
            raw_records_count=1,
            adapter_notes=None,
        )
    )
    await db_session.flush()

    o = await admin_repo.admin_overview(
        db_session, month_start=date(2026, 7, 1), month_before=date(2026, 8, 1)
    )
    # totals span both rows regardless of state (owner earned both)
    assert o["earned_total"] == Decimal("150")
    assert o["rebate_total"] == Decimal("138")
    assert o["profit_total"] == Decimal("12")
    # current month = only the July row
    assert o["earned_month"] == Decimal("100")
    assert o["rebate_month"] == Decimal("93")
    assert o["profit_month"] == Decimal("7")


# ---------------------------------------------------------------------------
# repository — commission override, user status, UID reassign/block (real Postgres)
# ---------------------------------------------------------------------------


@_async
async def test_set_commission_override_persists_pct_and_since(db_session: AsyncSession) -> None:
    await _user(db_session, 9701)

    await admin_repo.set_commission_override(db_session, 9701, Decimal("0.02"), date(2026, 7, 13))
    db_session.expire_all()

    user = await db_session.get(User, 9701)
    assert user is not None
    assert user.commission_override_pct == Decimal("0.02")
    assert user.commission_override_since == date(2026, 7, 13)


@_async
async def test_clear_commission_override_nulls_both_columns(db_session: AsyncSession) -> None:
    await _user(db_session, 9702)
    await admin_repo.set_commission_override(db_session, 9702, Decimal("0.05"), date(2026, 7, 1))

    await admin_repo.clear_commission_override(db_session, 9702)
    db_session.expire_all()

    user = await db_session.get(User, 9702)
    assert user is not None
    assert user.commission_override_pct is None
    assert user.commission_override_since is None


@_async
async def test_set_user_status_toggles_active_and_blocked(db_session: AsyncSession) -> None:
    await _user(db_session, 9703)

    await admin_repo.set_user_status(db_session, 9703, "blocked")
    db_session.expire_all()
    user = await db_session.get(User, 9703)
    assert user is not None
    assert user.status == "blocked"

    await admin_repo.set_user_status(db_session, 9703, "active")
    db_session.expire_all()
    user = await db_session.get(User, 9703)
    assert user is not None
    assert user.status == "active"


@_async
async def test_reassign_uid_moves_ownership_to_new_user(db_session: AsyncSession) -> None:
    await _user(db_session, 9704)  # old owner (A)
    await _user(db_session, 9705)  # new owner (B)
    acc = await _account(db_session, 9704, "bybit", "U9704")
    acc_id = acc.id

    ok = await admin_repo.reassign_uid(db_session, "bybit", "U9704", 9705)
    db_session.expire_all()

    assert ok is True
    refreshed = await db_session.get(ExchangeAccount, acc_id)
    assert refreshed is not None
    assert refreshed.user_id == 9705
    assert refreshed.status == "active"
    # Old owner no longer owns any (exchange, uid) row.
    assert refreshed.user_id != 9704


@_async
async def test_reassign_uid_false_when_no_such_account(db_session: AsyncSession) -> None:
    await _user(db_session, 9706)

    ok = await admin_repo.reassign_uid(db_session, "bybit", "NOPE", 9706)

    assert ok is False


@_async
async def test_block_uid_sets_status_blocked(db_session: AsyncSession) -> None:
    await _user(db_session, 9707)
    acc = await _account(db_session, 9707, "gate", "G9707")
    acc_id = acc.id

    ok = await admin_repo.block_uid(db_session, "gate", "G9707")
    db_session.expire_all()

    assert ok is True
    refreshed = await db_session.get(ExchangeAccount, acc_id)
    assert refreshed is not None
    assert refreshed.status == "blocked"


@_async
async def test_block_uid_false_when_already_blocked(db_session: AsyncSession) -> None:
    await _user(db_session, 9708)
    await _account(db_session, 9708, "gate", "G9708", status="blocked")

    ok = await admin_repo.block_uid(db_session, "gate", "G9708")

    assert ok is False


# ---------------------------------------------------------------------------
# service — pure parsing/validation
# ---------------------------------------------------------------------------


def test_parse_override_percent_accepts_in_range() -> None:
    # Admin types a PERCENT; the parser returns the stored FRACTION (percent/100).
    assert admin_service.parse_override_percent("2") == Decimal("0.02")
    assert admin_service.parse_override_percent("0") == Decimal("0")
    assert admin_service.parse_override_percent("100") == Decimal("1")
    assert admin_service.parse_override_percent("2.5") == Decimal("0.025")
    assert admin_service.parse_override_percent(" 5 ") == Decimal("0.05")


def test_parse_override_percent_rejects_out_of_range_or_garbage() -> None:
    assert admin_service.parse_override_percent("101") is None
    assert admin_service.parse_override_percent("-1") is None
    assert admin_service.parse_override_percent("abc") is None
    assert admin_service.parse_override_percent("") is None


def test_parse_rate_command_valid_and_clear() -> None:
    # Numeric target: still returned as the raw string — resolved to int by the router.
    # "2" percent -> 0.02 fraction.
    assert admin_service.parse_rate_command("12345 2") == ("12345", Decimal("0.02"))
    assert admin_service.parse_rate_command("12345 clear") == ("12345", None)


def test_parse_rate_command_accepts_nickname_target() -> None:
    # A nickname target is accepted verbatim — resolution against the DB is the
    # router's job (bot.botui.repository.find_user_id_by_username), not the parser's.
    assert admin_service.parse_rate_command("@nick 2") == ("@nick", Decimal("0.02"))
    assert admin_service.parse_rate_command("nick clear") == ("nick", None)


def test_parse_rate_command_rejects_bad_input() -> None:
    assert admin_service.parse_rate_command("12345 150") is None  # percent > 100
    assert admin_service.parse_rate_command("@nick 150") is None  # percent > 100
    assert admin_service.parse_rate_command("12345") is None
    assert admin_service.parse_rate_command("12345 2 extra") is None
    assert admin_service.parse_rate_command("12345 notanumber") is None


def test_parse_user_status_command() -> None:
    # Target is returned verbatim (nick or id) — the router resolves it.
    assert admin_service.parse_user_status_command("12345 blocked") == ("12345", "blocked")
    assert admin_service.parse_user_status_command("@nick active") == ("@nick", "active")
    assert admin_service.parse_user_status_command("12345 banned") is None
    assert admin_service.parse_user_status_command("12345") is None


def test_parse_reassign_command() -> None:
    # New-owner target is returned verbatim (nick or id) — the router resolves it.
    assert admin_service.parse_reassign_command("bybit U123 555") == ("bybit", "U123", "555")
    assert admin_service.parse_reassign_command("bybit U123 @nick") == ("bybit", "U123", "@nick")
    assert admin_service.parse_reassign_command("bybit U123") is None


def test_parse_block_uid_command() -> None:
    assert admin_service.parse_block_uid_command("gate G123") == ("gate", "G123")
    assert admin_service.parse_block_uid_command("gate") is None


# ---------------------------------------------------------------------------
# router — rate / block-user / reassign / block-uid handlers
# ---------------------------------------------------------------------------


@_async
async def test_request_rate_sets_state() -> None:
    state = _ctx(9999)
    callback = _callback(9999)

    await request_rate(callback, state)  # type: ignore[arg-type]

    assert await state.get_state() == AdminFlow.waiting_rate.state
    callback.answer.assert_awaited()


@_async
async def test_receive_rate_sets_override_and_audits(db_session: AsyncSession) -> None:
    await _user(db_session, 9709)

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "9709 2"
    message.from_user = SimpleNamespace(id=9999)

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    user = await db_session.get(User, 9709)
    assert user is not None
    assert user.commission_override_pct == Decimal("0.02")
    # since is the START of the current month (not raw today), so it also covers
    # already-accrued-but-unpaid rows earlier this month via the recompute step.
    assert user.commission_override_since == month_start(date.today())
    assert await state.get_state() is None


@_async
async def test_receive_rate_clear_nulls_override_and_audits(db_session: AsyncSession) -> None:
    await _user(db_session, 9710)
    await admin_repo.set_commission_override(db_session, 9710, Decimal("0.05"), date(2026, 7, 1))

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "9710 clear"
    message.from_user = SimpleNamespace(id=9999)

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    user = await db_session.get(User, 9710)
    assert user is not None
    assert user.commission_override_pct is None
    assert user.commission_override_since is None


@_async
async def test_receive_rate_invalid_input_reprompts(db_session: AsyncSession) -> None:
    await _user(db_session, 9711)

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "9711 150"  # percent > 100
    message.from_user = SimpleNamespace(id=9999)

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    assert await state.get_state() == AdminFlow.waiting_rate.state  # stays, unchanged
    user = await db_session.get(User, 9711)
    assert user is not None
    assert user.commission_override_pct is None


@_async
async def test_receive_rate_target_zero_is_not_a_user_id(db_session: AsyncSession) -> None:
    """FIX T12: "0 0.02" must NOT be treated as user_id=0 (int("0") > 0 is False) —
    it must fall through to username resolution (which fails for "0"), producing
    the not-found error reply with NO override written and NO audit event."""
    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "0 2"
    message.from_user = SimpleNamespace(id=9999)

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    (args, _kwargs) = message.answer.await_args
    assert "не найден" in args[0]
    user_zero = await db_session.get(User, 0)
    assert user_zero is None
    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "commission_override_set")
    )
    assert audit_count == 0


@_async
async def test_receive_rate_resolves_nickname_target(db_session: AsyncSession) -> None:
    """`@nick 0.02` resolves the nickname to its user id and sets the override there."""
    await _user(db_session, 9740, username="fooNick")

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "@foonick 2"  # resolution is case-insensitive, per find_user_id_by_username
    message.from_user = SimpleNamespace(id=9999)

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    user = await db_session.get(User, 9740)
    assert user is not None
    assert user.commission_override_pct == Decimal("0.02")
    assert await state.get_state() is None


@_async
async def test_receive_rate_unknown_nickname_reports_error_and_keeps_state(
    db_session: AsyncSession,
) -> None:
    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "@ghost 2"
    message.from_user = SimpleNamespace(id=9999)

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    (args, _kwargs) = message.answer.call_args
    assert "не найден" in args[0]
    assert "@ghost" in args[0]
    # No target was resolved, so nothing was cleared/set — the prompt stays active.
    assert await state.get_state() == AdminFlow.waiting_rate.state


@_async
async def test_receive_rate_recomputes_current_month_unpaid_rows(db_session: AsyncSession) -> None:
    """Setting 0.02 refreezes current-month available/accruing rows, leaves the rest.

    Covers: a plain USDT `available` row (fee 0.10→0.02, net 90→98), a `reserved`
    row and a `paid` row left untouched, and an MNT `available` row whose
    `net_native` is refrozen too.
    """
    await _user(db_session, 9741)
    acc = await _account(db_session, 9741, "gate", "G9741")
    m_start = month_start(date.today())

    avail = await _accrual(
        db_session,
        9741,
        acc.id,
        accrual_date=m_start,
        state="available",
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        amount_usdt=Decimal("100"),
    )
    reserved = await _accrual(
        db_session,
        9741,
        acc.id,
        accrual_date=m_start + timedelta(days=1),
        state="reserved",
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        amount_usdt=Decimal("100"),
    )
    paid = await _accrual(
        db_session,
        9741,
        acc.id,
        accrual_date=m_start + timedelta(days=2),
        state="paid",
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        amount_usdt=Decimal("100"),
    )
    mnt_avail = await _accrual(
        db_session,
        9741,
        acc.id,
        accrual_date=m_start + timedelta(days=3),
        state="available",
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        amount_usdt=Decimal("100"),
        asset="MNT",
        amount_native=Decimal("100"),
    )

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "9741 2"
    message.from_user = SimpleNamespace(id=9999)

    avail_id, reserved_id, paid_id, mnt_id = avail.id, reserved.id, paid.id, mnt_avail.id

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    db_session.expire_all()

    refreshed_avail = await db_session.get(RebateAccrual, avail_id)
    assert refreshed_avail is not None
    assert refreshed_avail.fee_pct == Decimal("0.02")
    assert refreshed_avail.commission_usdt == Decimal("2")
    assert refreshed_avail.net_usdt == Decimal("98")

    refreshed_reserved = await db_session.get(RebateAccrual, reserved_id)
    assert refreshed_reserved is not None
    assert refreshed_reserved.fee_pct == Decimal("0.10")
    assert refreshed_reserved.net_usdt == Decimal("90")

    refreshed_paid = await db_session.get(RebateAccrual, paid_id)
    assert refreshed_paid is not None
    assert refreshed_paid.fee_pct == Decimal("0.10")
    assert refreshed_paid.net_usdt == Decimal("90")

    refreshed_mnt = await db_session.get(RebateAccrual, mnt_id)
    assert refreshed_mnt is not None
    assert refreshed_mnt.fee_pct == Decimal("0.02")
    assert refreshed_mnt.net_native == Decimal("98")


@_async
async def test_receive_rate_clear_restores_rank_default_fee(db_session: AsyncSession) -> None:
    await _user(db_session, 9742)
    await admin_repo.set_commission_override(db_session, 9742, Decimal("0.02"), date(2026, 1, 1))
    acc = await _account(db_session, 9742, "gate", "G9742")
    m_start = month_start(date.today())
    row = await _accrual(
        db_session,
        9742,
        acc.id,
        accrual_date=m_start,
        state="available",
        effective_rank=2,
        fee_pct=Decimal("0.02"),
        amount_usdt=Decimal("100"),
    )

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "9742 clear"
    message.from_user = SimpleNamespace(id=9999)
    row_id = row.id

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    db_session.expire_all()
    user = await db_session.get(User, 9742)
    assert user is not None
    assert user.commission_override_pct is None

    refreshed = await db_session.get(RebateAccrual, row_id)
    assert refreshed is not None
    assert refreshed.fee_pct == rank_fee_pct(2)
    assert refreshed.commission_usdt == Decimal("100") * rank_fee_pct(2)
    assert refreshed.net_usdt == Decimal("100") - refreshed.commission_usdt


@_async
async def test_receive_rate_skips_rank_zero_current_month_row(db_session: AsyncSession) -> None:
    await _user(db_session, 9743)
    acc = await _account(db_session, 9743, "gate", "G9743")
    m_start = month_start(date.today())
    row = await _accrual(
        db_session,
        9743,
        acc.id,
        accrual_date=m_start,
        state="accruing",
        effective_rank=0,
        fee_pct=Decimal("0"),
        amount_usdt=Decimal("5"),
    )

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_rate)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "9743 2"
    message.from_user = SimpleNamespace(id=9999)
    row_id = row.id

    await receive_rate(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    db_session.expire_all()
    refreshed = await db_session.get(RebateAccrual, row_id)
    assert refreshed is not None
    assert refreshed.fee_pct == Decimal("0")
    assert refreshed.commission_usdt == Decimal("0")
    assert refreshed.net_usdt == Decimal("5")


@_async
async def test_show_user_controls_answers_with_markup() -> None:
    callback = _callback(9999)

    await show_user_controls(callback, _ctx(9999))  # type: ignore[arg-type]

    callback.answer.assert_awaited()
    message = callback.message
    message.edit_text.assert_awaited()
    _args, kwargs = message.edit_text.call_args
    assert "reply_markup" in kwargs


@_async
async def test_receive_user_status_blocks_and_audits(db_session: AsyncSession) -> None:
    await _user(db_session, 9712)

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_user_status)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "9712 blocked"
    message.from_user = SimpleNamespace(id=9999)

    await receive_user_status(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    user = await db_session.get(User, 9712)
    assert user is not None
    assert user.status == "blocked"
    assert await state.get_state() is None


@_async
async def test_receive_user_status_resolves_nickname(db_session: AsyncSession) -> None:
    """`@nick blocked` resolves the nickname to its user id and blocks it (admins
    only have @nicks, not user ids)."""
    await _user(db_session, 9722, username="badguy")

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_user_status)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "@BadGuy blocked"  # case-insensitive resolution
    message.from_user = SimpleNamespace(id=9999)

    await receive_user_status(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    user = await db_session.get(User, 9722)
    assert user is not None
    assert user.status == "blocked"
    assert await state.get_state() is None


@_async
async def test_receive_user_status_unknown_nickname_errors(db_session: AsyncSession) -> None:
    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_user_status)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "@ghost blocked"
    message.from_user = SimpleNamespace(id=9999)

    await receive_user_status(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    (args, _kwargs) = message.answer.call_args
    assert "не найден" in args[0]
    assert "@ghost" in args[0]


@_async
async def test_receive_reassign_resolves_nickname_target(db_session: AsyncSession) -> None:
    """`<exchange> <uid> @nick` reassigns the UID to the nickname's user id."""
    await _user(db_session, 9730)  # old owner
    await _user(db_session, 9731, username="newowner")  # new owner, by nick
    acc = await _account(db_session, 9730, "okx", "O9730")

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_reassign)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "okx O9730 @NewOwner"
    message.from_user = SimpleNamespace(id=9999)

    await receive_reassign(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    refreshed = await db_session.get(ExchangeAccount, acc.id)
    assert refreshed is not None
    assert refreshed.user_id == 9731
    assert await state.get_state() is None


@_async
async def test_receive_reassign_moves_ownership_and_audits(db_session: AsyncSession) -> None:
    await _user(db_session, 9713)
    await _user(db_session, 9714)
    acc = await _account(db_session, 9713, "okx", "O9713")

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_reassign)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "okx O9713 9714"
    message.from_user = SimpleNamespace(id=9999)

    await receive_reassign(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    refreshed = await db_session.get(ExchangeAccount, acc.id)
    assert refreshed is not None
    assert refreshed.user_id == 9714
    assert refreshed.status == "active"
    assert await state.get_state() is None


@_async
async def test_receive_reassign_not_found_reports_and_clears_state(
    db_session: AsyncSession,
) -> None:
    await _user(db_session, 9715)

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_reassign)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "okx NOPE 9715"
    message.from_user = SimpleNamespace(id=9999)

    await receive_reassign(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    (args, _kwargs) = message.answer.call_args
    assert "не найден" in args[0].lower()


@_async
async def test_receive_block_uid_blocks_and_audits(db_session: AsyncSession) -> None:
    await _user(db_session, 9716)
    acc = await _account(db_session, 9716, "mexc", "M9716")

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_block_uid)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "mexc M9716"
    message.from_user = SimpleNamespace(id=9999)

    await receive_block_uid(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    refreshed = await db_session.get(ExchangeAccount, acc.id)
    assert refreshed is not None
    assert refreshed.status == "blocked"
    assert await state.get_state() is None


@_async
async def test_request_block_uid_sets_state() -> None:
    state = _ctx(9999)
    callback = _callback(9999)

    await request_block_uid(callback, state)  # type: ignore[arg-type]

    assert await state.get_state() == AdminFlow.waiting_block_uid.state


@_async
async def test_request_user_status_sets_state() -> None:
    state = _ctx(9999)
    callback = _callback(9999)

    await request_user_status(callback, state)  # type: ignore[arg-type]

    assert await state.get_state() == AdminFlow.waiting_user_status.state


@_async
async def test_request_reassign_sets_state() -> None:
    state = _ctx(9999)
    callback = _callback(9999)

    await request_reassign(callback, state)  # type: ignore[arg-type]

    assert await state.get_state() == AdminFlow.waiting_reassign.state


# ---------------------------------------------------------------------------
# 💰 Баланс кошелька — hot payout wallet on-chain balances
# ---------------------------------------------------------------------------


@_async
async def test_show_wallet_balance_renders_onchain_balances(monkeypatch) -> None:
    from bot.executor.chain_web3 import Web3ChainClient

    key = "0x" + "1" * 64
    bsc = Web3ChainClient(
        rpc_urls=["http://localhost:0"], private_key=key, usdt_contract="0x" + "0" * 40, chain_id=56
    )
    bsc.usdt_balance = AsyncMock(return_value=Decimal("1234.5"))
    bsc.native_balance = AsyncMock(return_value=Decimal("0.25"))
    mantle = Web3ChainClient(
        rpc_urls=["http://localhost:0"], private_key=key, usdt_contract=None, chain_id=5000
    )
    mantle.native_balance = AsyncMock(return_value=Decimal("24.4559"))
    monkeypatch.setattr("bot.admin.router.build_chain_client", lambda s: bsc)
    monkeypatch.setattr("bot.admin.router.build_mantle_chain_client", lambda s: mantle)

    cb = _callback(9820)
    await show_wallet_balance(cb, _ctx(9999))  # type: ignore[arg-type]

    cb.answer.assert_awaited()
    text = cb.message.edit_text.await_args.args[0]
    assert "Баланс кошелька" in text
    assert "1234.50" in text  # USDT (format_2dp)
    assert "0.250000" in text  # BNB gas
    assert "24.455900" in text  # MNT
    assert bsc.address in text


@_async
async def test_show_wallet_balance_reports_disabled(monkeypatch) -> None:
    from bot.executor.chain import MockChainClient

    monkeypatch.setattr("bot.admin.router.build_chain_client", lambda s: MockChainClient())
    monkeypatch.setattr("bot.admin.router.build_mantle_chain_client", lambda s: None)

    cb = _callback(9821)
    await show_wallet_balance(cb, _ctx(9999))  # type: ignore[arg-type]

    text = cb.message.edit_text.await_args.args[0]
    assert "не настроены" in text
    assert "выключено" in text
