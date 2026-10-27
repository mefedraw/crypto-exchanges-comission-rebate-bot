# tests/admin/test_release.py
"""Task 4: release/reject pending_admin payouts + one-time commission reduction."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin import repository as admin_repo
from bot.admin import service as admin_service
from bot.admin.keyboards import PayoutActionCB, payout_action_keyboard
from bot.admin.router import (
    _payout_summary,
    receive_new_commission,
    reject_action,
    release_action,
    request_reduce,
    show_pending,
)
from bot.admin.states import AdminFlow
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.payouts import Payout

_async = pytest.mark.asyncio(loop_scope="session")


async def _seed_admin(session: AsyncSession, admin_id: int) -> None:
    """`overridden_by` is FK'd to `users.id`; seed the acting admin as a user row."""
    session.add(User(id=admin_id, role="admin", status="active"))
    await session.flush()


async def _user_with_wallet(session: AsyncSession, user_id: int) -> tuple[ExchangeAccount, Wallet]:
    session.add(User(id=user_id, role="user", status="active"))
    await session.flush()
    wallet = Wallet(
        user_id=user_id,
        address_encrypted=b"enc",
        address_hash=f"hash{user_id}".encode(),
        address_masked="0x1234…abcd",
    )
    session.add(wallet)
    acc = ExchangeAccount(
        user_id=user_id,
        exchange="bybit",
        uid=f"U{user_id}",
        status="active",
        accrual_since=date(2026, 7, 1),
    )
    session.add(acc)
    await session.flush()
    return acc, wallet


async def _payout(
    session: AsyncSession,
    user_id: int,
    wallet_id: int,
    status: str,
    *,
    gross: Decimal = Decimal("300"),
    commission: Decimal = Decimal("30"),
    net: Decimal = Decimal("270"),
    asset: str = "USDT",
    net_native: Decimal = Decimal("0"),
) -> Payout:
    payout = Payout(
        user_id=user_id,
        wallet_id=wallet_id,
        address_encrypted=b"x",
        address_masked="0x..abcd",
        covered_from=date(2026, 7, 1),
        covered_to=date(2026, 7, 31),
        gross_usdt=gross,
        commission_usdt=commission,
        net_usdt=net,
        asset=asset,
        net_native=net_native,
        status=status,
        idempotency_key=f"idem-{user_id}-{status}-{gross}-{commission}",
    )
    session.add(payout)
    await session.flush()
    return payout


def _accrual(
    user_id: int,
    account_id: int,
    d: date,
    amount: Decimal,
    state: str,
    payout_id: int | None,
) -> RebateAccrual:
    fee = Decimal("0.10")
    commission = amount * fee
    return RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=d,
        amount_usdt=amount,
        effective_rank=1,
        fee_pct=fee,
        commission_usdt=commission,
        net_usdt=amount - commission,
        state=state,
        payout_id=payout_id,
        available_at=datetime(2026, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 10, tzinfo=UTC),
        raw_records_count=1,
        adapter_notes=None,
    )


# ---------------------------------------------------------------------------
# repository — release / reject / reduce (real Postgres)
# ---------------------------------------------------------------------------


@_async
async def test_release_payout_flips_pending_to_reserved(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9601)
    await _seed_admin(db_session, 9999)
    payout = await _payout(db_session, 9601, wallet.id, "pending_admin")
    payout_id = payout.id

    ok = await admin_repo.release_payout(db_session, payout_id, 9999)
    await db_session.flush()
    db_session.expire_all()

    assert ok is True
    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "reserved"
    assert refreshed.approved_by == 9999
    assert refreshed.approved_at is not None


@_async
async def test_release_payout_false_when_not_pending(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9602)
    await _seed_admin(db_session, 9999)
    payout = await _payout(db_session, 9602, wallet.id, "reserved")

    ok = await admin_repo.release_payout(db_session, payout.id, 9999)

    assert ok is False


@_async
async def test_reject_payout_cancels_and_frees_accruals(db_session: AsyncSession) -> None:
    acc, wallet = await _user_with_wallet(db_session, 9603)
    payout = await _payout(db_session, 9603, wallet.id, "pending_admin")
    payout_id = payout.id
    db_session.add(_accrual(9603, acc.id, date(2026, 7, 5), Decimal("150"), "reserved", payout_id))
    db_session.add(_accrual(9603, acc.id, date(2026, 7, 6), Decimal("150"), "reserved", payout_id))
    await db_session.flush()

    ok = await admin_repo.reject_payout(db_session, payout_id)
    await db_session.flush()
    db_session.expire_all()

    assert ok is True
    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "cancelled"

    accruals = (
        await db_session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 9603))
    ).all()
    assert len(accruals) == 2
    for a in accruals:
        assert a.state == "available"
        assert a.payout_id is None


@_async
async def test_reject_payout_false_when_not_pending(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9604)
    payout = await _payout(db_session, 9604, wallet.id, "reserved")

    ok = await admin_repo.reject_payout(db_session, payout.id)

    assert ok is False


@_async
async def test_reduce_payout_commission_lowers_and_recomputes_net(
    db_session: AsyncSession,
) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9605)
    await _seed_admin(db_session, 9999)
    payout = await _payout(
        db_session,
        9605,
        wallet.id,
        "pending_admin",
        gross=Decimal("300"),
        commission=Decimal("30"),
        net=Decimal("270"),
    )
    payout_id = payout.id

    ok = await admin_repo.reduce_payout_commission(
        db_session, payout_id, Decimal("10"), 9999, "goodwill discount"
    )
    await db_session.flush()
    db_session.expire_all()

    assert ok is True
    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.commission_usdt == Decimal("30")  # original untouched
    assert refreshed.commission_override_usdt == Decimal("10")
    assert refreshed.overridden_by == 9999
    assert refreshed.override_reason == "goodwill discount"
    assert refreshed.net_usdt == Decimal("290")  # 300 - 10 - 0 gas (net goes up)


@_async
async def test_reduce_payout_commission_rejects_raise(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9606)
    payout = await _payout(
        db_session,
        9606,
        wallet.id,
        "pending_admin",
        gross=Decimal("300"),
        commission=Decimal("30"),
        net=Decimal("270"),
    )
    payout_id = payout.id

    ok = await admin_repo.reduce_payout_commission(
        db_session, payout_id, Decimal("50"), 9999, "oops raise"
    )
    db_session.expire_all()

    assert ok is False
    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.commission_override_usdt is None
    assert refreshed.net_usdt == Decimal("270")


@_async
async def test_reduce_payout_commission_false_when_not_pending(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9607)
    payout = await _payout(db_session, 9607, wallet.id, "reserved")

    ok = await admin_repo.reduce_payout_commission(db_session, payout.id, Decimal("1"), 9999, "x")

    assert ok is False


# ---------------------------------------------------------------------------
# router — pending list + release/reject/reduce handlers
# ---------------------------------------------------------------------------


def _message() -> Message:
    # Real (frozen) Message so isinstance + panel.remember(msg.chat.id) work; async
    # methods are mocked via object.__setattr__ (the model is frozen).
    msg = Message.model_construct(message_id=1, chat=Chat.model_construct(id=1, type="private"))
    for _name in ("answer", "edit_text", "delete"):
        object.__setattr__(msg, _name, AsyncMock())
    return msg


def _ctx(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


@_async
async def test_show_pending_lists_payout_with_keyboard(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9608)
    payout = await _payout(db_session, 9608, wallet.id, "pending_admin")

    message = _message()
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=1), answer=AsyncMock())
    await show_pending(callback, _ctx(9999), db_session)  # type: ignore[arg-type]

    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()
    (args, kwargs) = message.edit_text.call_args
    assert str(payout.id) in args[0]
    assert "reply_markup" in kwargs


@_async
async def test_show_pending_empty_reports_none(db_session: AsyncSession) -> None:
    message = _message()
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=1), answer=AsyncMock())
    await show_pending(callback, _ctx(9999), db_session)  # type: ignore[arg-type]

    message.edit_text.assert_awaited()
    (args, _kwargs) = message.edit_text.call_args
    assert "нет" in args[0].lower()


@_async
async def test_payout_summary_mnt_shows_asset_and_native_amount(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9616)
    payout = await _payout(
        db_session,
        9616,
        wallet.id,
        "pending_admin",
        gross=Decimal("300"),
        commission=Decimal("30"),
        net=Decimal("270"),
        asset="MNT",
        net_native=Decimal("540.5"),
    )

    text = _payout_summary(payout)

    assert "Актив: MNT" in text
    assert "540.50 MNT" in text
    assert "≈270.00 USDT" in text


@_async
async def test_payout_summary_usdt_keeps_current_net_line(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9617)
    payout = await _payout(db_session, 9617, wallet.id, "pending_admin")

    text = _payout_summary(payout)

    assert "Актив: USDT" in text
    assert "Net: 270.00 USDT" in text
    assert "MNT" not in text


def test_payout_action_keyboard_mnt_release_label() -> None:
    markup = payout_action_keyboard(1, "MNT")
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "✅ Пополнил кошелёк MNT — отправить" in labels


def test_payout_action_keyboard_usdt_release_label() -> None:
    markup = payout_action_keyboard(1, "USDT")
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "✅ Одобрить" in labels


@_async
async def test_show_pending_uses_mnt_release_label_for_mnt_payout(
    db_session: AsyncSession,
) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9618)
    await _payout(
        db_session,
        9618,
        wallet.id,
        "pending_admin",
        asset="MNT",
        net_native=Decimal("12.3"),
    )

    message = _message()
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=1), answer=AsyncMock())
    await show_pending(callback, _ctx(9999), db_session)  # type: ignore[arg-type]

    (_args, kwargs) = message.edit_text.call_args
    markup = kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "✅ Пополнил кошелёк MNT — отправить" in labels


@_async
async def test_release_action_flips_status_and_audits(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9609)
    await _seed_admin(db_session, 9999)
    payout = await _payout(db_session, 9609, wallet.id, "pending_admin")
    payout_id = payout.id

    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=9999), answer=AsyncMock()
    )
    await release_action(
        callback, PayoutActionCB(action="release", payout_id=payout_id), _ctx(9999), db_session
    )  # type: ignore[arg-type]

    callback.answer.assert_awaited()
    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "reserved"
    assert refreshed.approved_by == 9999
    assert refreshed.approved_at is not None


@_async
async def test_release_action_already_handled_reports_soft_message(
    db_session: AsyncSession,
) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9615)
    payout = await _payout(db_session, 9615, wallet.id, "reserved")

    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=9999), answer=AsyncMock()
    )
    await release_action(
        callback, PayoutActionCB(action="release", payout_id=payout.id), _ctx(9999), db_session
    )  # type: ignore[arg-type]

    callback.answer.assert_awaited()  # soft "already handled" is a toast now


@_async
async def test_reject_action_cancels_and_frees_accruals(db_session: AsyncSession) -> None:
    acc, wallet = await _user_with_wallet(db_session, 9610)
    payout = await _payout(db_session, 9610, wallet.id, "pending_admin")
    payout_id = payout.id
    db_session.add(_accrual(9610, acc.id, date(2026, 7, 5), Decimal("150"), "reserved", payout_id))
    await db_session.flush()

    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=9999), answer=AsyncMock()
    )
    await reject_action(
        callback, PayoutActionCB(action="reject", payout_id=payout_id), _ctx(9999), db_session
    )  # type: ignore[arg-type]

    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "cancelled"

    accruals = (
        await db_session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 9610))
    ).all()
    assert accruals[0].state == "available"
    assert accruals[0].payout_id is None


@_async
async def test_request_reduce_sets_state_with_payout_id(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9611)
    payout = await _payout(db_session, 9611, wallet.id, "pending_admin")
    payout_id = payout.id

    state = _ctx(9999)
    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=9999), answer=AsyncMock()
    )
    await request_reduce(callback, PayoutActionCB(action="reduce", payout_id=payout_id), state)  # type: ignore[arg-type]

    assert await state.get_state() == AdminFlow.waiting_commission.state
    data = await state.get_data()
    assert data.get("payout_id") == payout_id


@_async
async def test_receive_new_commission_reduces_and_releases(db_session: AsyncSession) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9612)
    await _seed_admin(db_session, 9999)
    payout = await _payout(
        db_session,
        9612,
        wallet.id,
        "pending_admin",
        gross=Decimal("300"),
        commission=Decimal("30"),
        net=Decimal("270"),
    )
    payout_id = payout.id

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_commission)
    await state.update_data(payout_id=payout_id)

    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "10"
    message.from_user = SimpleNamespace(id=9999)

    await receive_new_commission(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    message.answer.assert_awaited()
    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "reserved"
    assert refreshed.commission_override_usdt == Decimal("10")
    assert refreshed.net_usdt == Decimal("290")
    assert refreshed.approved_by == 9999
    assert refreshed.approved_at is not None
    assert await state.get_state() is None


@_async
async def test_receive_new_commission_rejects_raise_and_keeps_pending(
    db_session: AsyncSession,
) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9613)
    payout = await _payout(
        db_session,
        9613,
        wallet.id,
        "pending_admin",
        gross=Decimal("300"),
        commission=Decimal("30"),
        net=Decimal("270"),
    )
    payout_id = payout.id

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_commission)
    await state.update_data(payout_id=payout_id)

    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "50"
    message.from_user = SimpleNamespace(id=9999)

    await receive_new_commission(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "pending_admin"  # unchanged, never released
    assert refreshed.commission_override_usdt is None


@_async
async def test_receive_new_commission_invalid_text_prompts_again(
    db_session: AsyncSession,
) -> None:
    _acc, wallet = await _user_with_wallet(db_session, 9614)
    payout = await _payout(db_session, 9614, wallet.id, "pending_admin")
    payout_id = payout.id

    state = _ctx(9999)
    await state.set_state(AdminFlow.waiting_commission)
    await state.update_data(payout_id=payout_id)

    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.text = "not a number"
    message.from_user = SimpleNamespace(id=9999)

    await receive_new_commission(message, state, db_session, AsyncMock())  # type: ignore[arg-type]

    assert await state.get_state() == AdminFlow.waiting_commission.state
    refreshed = await db_session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "pending_admin"


# ---------------------------------------------------------------------------
# service — pure parsing/validation
# ---------------------------------------------------------------------------


def test_parse_commission_amount_valid() -> None:
    assert admin_service.parse_commission_amount("10.5") == Decimal("10.5")
    assert admin_service.parse_commission_amount("0") == Decimal("0")
    assert admin_service.parse_commission_amount(" 3,25 ") == Decimal("3.25")


def test_parse_commission_amount_invalid() -> None:
    assert admin_service.parse_commission_amount("abc") is None
    assert admin_service.parse_commission_amount("-5") is None
    assert admin_service.parse_commission_amount("") is None


@_async
async def test_admin_overview_paid_split_by_asset(db_session: AsyncSession) -> None:
    """All-time wallet payouts split into USDT + native MNT; only status='paid' counts."""
    _, wallet = await _user_with_wallet(db_session, 9640)
    await _payout(
        db_session,
        9640,
        wallet.id,
        "paid",
        gross=Decimal("110"),
        commission=Decimal("10"),
        net=Decimal("100"),
        asset="USDT",
    )
    await _payout(
        db_session,
        9640,
        wallet.id,
        "paid",
        gross=Decimal("55"),
        commission=Decimal("5"),
        net=Decimal("50"),
        asset="MNT",
        net_native=Decimal("90"),
    )
    # a non-paid payout must NOT count toward the wallet total
    await _payout(
        db_session,
        9640,
        wallet.id,
        "reserved",
        gross=Decimal("999"),
        commission=Decimal("0"),
        net=Decimal("999"),
        asset="USDT",
    )

    o = await admin_repo.admin_overview(
        db_session, month_start=date(2026, 8, 1), month_before=date(2026, 9, 1)
    )
    assert o["paid_count"] == 2
    assert o["paid_sum"] == Decimal("150")  # 100 + 50 (net_usdt over both assets)
    assert o["paid_mnt_native"] == Decimal("90")  # native MNT actually sent
    assert o["paid_mnt_usdt"] == Decimal("50")  # USDT-equiv of the MNT payout
    assert o["paid_usdt"] == Decimal("100")  # USDT part = total − MNT part
