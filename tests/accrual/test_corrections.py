from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.accrual import repository as repo
from bot.accrual.corrections import GateCorrectionPlan, basis_sha256, post_gate_correction
from bot.admin.repository import recompute_current_month_unpaid
from bot.botui import repository as cabinet_repo
from bot.botui.service import balance_summary
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


async def _seed(session, user_id=991001):
    session.add(User(id=user_id, role="user", status="active"))
    await session.flush()
    account = ExchangeAccount(
        user_id=user_id,
        exchange="gate",
        uid=str(user_id),
        status="active",
        accrual_since=date(2026, 7, 10),
    )
    session.add(account)
    await session.flush()
    for day, amount, state in [(8, "100", "paid"), (25, "20", "available")]:
        gross = Decimal(amount)
        await repo.upsert_accrual(
            session,
            user_id=user_id,
            account_id=account.id,
            accrual_date=date(2026, 10, day),
            amount_usdt=gross,
            effective_rank=1,
            fee_pct=Decimal("0.1"),
            commission_usdt=gross * Decimal("0.1"),
            net_usdt=gross * Decimal("0.9"),
            state=state,
            available_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=10),
            raw_records_count=1,
            adapter_notes="original",
        )
    rows = list(
        (
            await session.scalars(
                select(RebateAccrual).where(RebateAccrual.account_id == account.id)
            )
        ).all()
    )
    plan = GateCorrectionPlan(
        correction_key=f"gate:{user_id}:reconcile-v1",
        account_id=account.id,
        user_id=user_id,
        uid=str(user_id),
        period_month=date(2026, 11, 1),
        source_from=datetime(2026, 10, 1, tzinfo=UTC),
        source_to=datetime(2026, 11, 10, 23, 59, 59, tzinfo=UTC),
        expected_gross=Decimal("200"),
        expected_net=Decimal("185"),
        basis_sha256=basis_sha256(rows),
        effective_rank=1,
    )
    return plan, rows


async def test_correction_credits_exact_delta_and_preserves_originals(db_session):
    plan, rows = await _seed(db_session)
    before = [(row.id, row.amount_usdt, row.net_usdt, row.state) for row in rows]
    receipt = await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
    assert receipt.created and receipt.gross_delta == Decimal("80")
    assert receipt.net_delta == Decimal("77")
    for row, values in zip(rows, before, strict=True):
        await db_session.refresh(row)
        assert (row.id, row.amount_usdt, row.net_usdt, row.state) == values
    credit = await db_session.get(RebateAccrual, receipt.accrual_id)
    assert credit.fee_pct is None
    assert credit.net_native == Decimal("77")
    assert credit.commission_usdt == Decimal("3")
    assert credit.amount_usdt - credit.commission_usdt == credit.net_usdt
    assert credit.available_at == NOW and credit.expires_at == NOW + timedelta(days=30)
    summary = await balance_summary(db_session, plan.user_id, NOW, NOW.date())
    assert summary.available_net == Decimal("95")
    assert await repo.last_finalized_day(db_session, plan.account_id) == date(2026, 10, 25)
    assert await repo.last_gate_query_end(db_session, plan.account_id) == datetime(
        2026, 10, 25, 23, 59, 59, tzinfo=UTC
    )
    assert (
        await repo.finalized_amounts(
            db_session, plan.account_id, date(2026, 11, 1), date(2026, 12, 1)
        )
        == {}
    )
    assert await repo.user_month_gross(
        db_session, plan.user_id, date(2026, 11, 1), date(2026, 12, 1)
    ) == Decimal("80")


async def test_retry_is_idempotent_and_writes_one_audit_event(db_session):
    plan, _ = await _seed(db_session)
    first = await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
    second = await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
    assert first.created and not second.created
    assert first.accrual_id == second.accrual_id
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "gate_reconciliation_credit")
        )
        == 1
    )


async def test_basis_drift_and_overlapping_reconciliation_are_rejected(db_session):
    plan, _ = await _seed(db_session)
    with pytest.raises(ValueError, match="amounts changed"):
        await post_gate_correction(
            db_session, replace(plan, basis_sha256="0" * 64), actor="owner-request", now=NOW
        )
    await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
    with pytest.raises(ValueError, match="already covers"):
        await post_gate_correction(
            db_session,
            replace(plan, correction_key=plan.correction_key + "-again"),
            actor="owner-request",
            now=NOW,
        )


async def test_fee_override_does_not_reprice_compensation(db_session):
    plan, _ = await _seed(db_session)
    receipt = await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
    count = await recompute_current_month_unpaid(
        db_session, plan.user_id, Decimal("0.05"), date(2026, 10, 1), date(2026, 12, 1)
    )
    assert count == 1
    credit = await db_session.get(RebateAccrual, receipt.accrual_id)
    await db_session.refresh(credit)
    assert credit.net_usdt == Decimal("77") and credit.fee_pct is None


async def test_normal_accrual_can_coexist_with_correction_and_still_upsert(db_session):
    plan, _ = await _seed(db_session)
    receipt = await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
    for gross in (Decimal("10"), Decimal("15")):
        await repo.upsert_accrual(
            db_session,
            user_id=plan.user_id,
            account_id=plan.account_id,
            accrual_date=plan.period_month,
            amount_usdt=gross,
            effective_rank=1,
            fee_pct=Decimal("0.1"),
            commission_usdt=gross * Decimal("0.1"),
            net_usdt=gross * Decimal("0.9"),
            state="available",
            available_at=NOW,
            expires_at=NOW + timedelta(days=30),
            raw_records_count=1,
            adapter_notes="real API accrual",
        )
    rows = (
        await db_session.scalars(
            select(RebateAccrual).where(
                RebateAccrual.account_id == plan.account_id,
                RebateAccrual.accrual_date == plan.period_month,
            )
        )
    ).all()
    assert len(rows) == 2
    credit = next(row for row in rows if row.id == receipt.accrual_id)
    assert credit.net_usdt == Decimal("77")
    normal = next(row for row in rows if row.correction_key is None)
    assert normal.amount_usdt == Decimal("15")
    assert await repo.finalized_amounts(
        db_session, plan.account_id, date(2026, 11, 1), date(2026, 12, 1)
    ) == {date(2026, 11, 1): Decimal("15")}


async def test_correction_and_audit_roll_back_together(db_session):
    plan, _ = await _seed(db_session)
    with pytest.raises(RuntimeError, match="rollback"):
        async with db_session.begin_nested():
            await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
            raise RuntimeError("rollback")
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RebateAccrual)
            .where(RebateAccrual.correction_key == plan.correction_key)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "gate_reconciliation_credit")
        )
        == 0
    )


async def test_credit_can_be_reserved_by_the_existing_payout_flow(db_session):
    plan, _ = await _seed(db_session)
    receipt = await post_gate_correction(db_session, plan, actor="owner-request", now=NOW)
    wallet = Wallet(
        user_id=plan.user_id,
        chain="BEP20",
        address_encrypted=b"test-encrypted",
        address_hash=b"test-hash",
        address_masked="0x1234...5678",
        status="active",
    )
    db_session.add(wallet)
    await db_session.flush()
    available = await cabinet_repo.available_accruals(db_session, plan.user_id, asset="USDT")
    ids = [row.id for row in available]
    assert receipt.accrual_id in ids
    payout = await cabinet_repo.reserve_accruals_and_create_payout(
        db_session,
        plan.user_id,
        ids,
        wallet=wallet,
        gross=sum((row.amount_usdt for row in available), Decimal(0)),
        commission=sum((row.commission_usdt for row in available), Decimal(0)),
        net=sum((row.net_usdt for row in available), Decimal(0)),
        net_native=sum((row.net_native for row in available), Decimal(0)),
        idempotency_key="correction-reservation-test",
    )
    assert payout is not None and payout.net_usdt == Decimal("95")
    credit = await db_session.get(RebateAccrual, receipt.accrual_id)
    await db_session.refresh(credit)
    assert credit.state == "reserved" and credit.payout_id == payout.id


async def test_concurrent_requests_only_post_once(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as session:
        plan, _ = await _seed(session, user_id=991100)
        await session.commit()

    async def post():
        async with sm() as session, session.begin():
            return await post_gate_correction(session, plan, actor="owner-request", now=NOW)

    try:
        receipts = await asyncio.gather(post(), post())
        assert sorted(receipt.created for receipt in receipts) == [False, True]
        assert receipts[0].accrual_id == receipts[1].accrual_id
    finally:
        async with sm() as session, session.begin():
            await session.execute(
                delete(AuditEvent).where(
                    AuditEvent.new_value["correction_key"].astext == plan.correction_key
                )
            )
            await session.execute(
                delete(RebateAccrual).where(RebateAccrual.account_id == plan.account_id)
            )
            await session.execute(
                delete(ExchangeAccount).where(ExchangeAccount.id == plan.account_id)
            )
            await session.execute(delete(User).where(User.id == plan.user_id))
