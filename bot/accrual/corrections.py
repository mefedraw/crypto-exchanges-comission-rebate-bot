"""Post explicitly authorized Gate reconciliation credits without altering accruals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent


@dataclass(frozen=True)
class GateCorrectionPlan:
    correction_key: str
    account_id: int
    user_id: int
    uid: str
    period_month: date
    source_from: datetime
    source_to: datetime
    expected_gross: Decimal
    expected_net: Decimal
    basis_sha256: str
    effective_rank: int


@dataclass(frozen=True)
class CorrectionReceipt:
    accrual_id: int
    created: bool
    gross_delta: Decimal
    net_delta: Decimal


def basis_sha256(rows: Sequence[RebateAccrual]) -> str:
    payload = [
        {
            "id": str(row.id),
            "accrual_date": str(row.accrual_date),
            "asset": row.asset,
            "amount_usdt": str(row.amount_usdt),
            "commission_usdt": str(row.commission_usdt),
            "net_usdt": str(row.net_usdt),
        }
        for row in sorted(rows, key=lambda row: (row.accrual_date, row.id))
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _metadata(plan: GateCorrectionPlan) -> str:
    return json.dumps(
        {
            "kind": "authorized_gate_reconciliation",
            "correction_key": plan.correction_key,
            "uid": plan.uid,
            "source_from": plan.source_from.astimezone(UTC).isoformat(),
            "source_to": plan.source_to.astimezone(UTC).isoformat(),
            "expected_gross": str(plan.expected_gross),
            "expected_net": str(plan.expected_net),
            "basis_sha256": plan.basis_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _covered_rows(rows: Sequence[RebateAccrual], plan: GateCorrectionPlan) -> list[RebateAccrual]:
    covered = []
    for row in rows:
        start = row.query_from or datetime.combine(row.accrual_date, time.min, UTC)
        end = row.query_to or (
            datetime.combine(row.accrual_date + timedelta(days=1), time.min, UTC)
            - timedelta(seconds=1)
        )
        if end < plan.source_from or start > plan.source_to:
            continue
        if start < plan.source_from or end > plan.source_to:
            raise ValueError("A source accrual straddles the reconciliation boundary.")
        if row.asset != "USDT" or not row.finalized:
            raise ValueError("Source accrual must be finalized USDT.")
        covered.append(row)
    return covered


async def post_gate_correction(
    session: AsyncSession, plan: GateCorrectionPlan, *, actor: str, now: datetime
) -> CorrectionReceipt:
    """Caller commits once; the account lock and unique key make retries idempotent."""
    if not plan.uid.isdigit() or not 1 <= len(plan.correction_key) <= 160:
        raise ValueError("Invalid Gate correction identity.")
    if plan.period_month.day != 1 or plan.effective_rank not in (1, 2, 3, 4):
        raise ValueError("Invalid correction month or rank.")
    if any(dt.utcoffset() is None for dt in (plan.source_from, plan.source_to, now)):
        raise ValueError("Correction timestamps must be timezone-aware.")
    if plan.source_from > plan.source_to or not actor:
        raise ValueError("Invalid correction period or actor.")
    if not all(value.is_finite() for value in (plan.expected_gross, plan.expected_net)):
        raise ValueError("Correction amounts must be finite.")
    if not Decimal(0) <= plan.expected_net <= plan.expected_gross:
        raise ValueError("Invalid canonical entitlement.")

    account = await session.scalar(
        select(ExchangeAccount).where(ExchangeAccount.id == plan.account_id).with_for_update()
    )
    if account is None or (account.user_id, account.exchange, account.uid) != (
        plan.user_id,
        "gate",
        plan.uid,
    ):
        raise ValueError("Gate account ownership changed.")

    notes = _metadata(plan)
    previous = await session.scalar(
        select(RebateAccrual).where(RebateAccrual.correction_key == plan.correction_key)
    )
    if previous is not None:
        if previous.account_id != plan.account_id or previous.adapter_notes != notes:
            raise ValueError("Correction key was already used for a different plan.")
        return CorrectionReceipt(previous.id, False, previous.amount_usdt, previous.net_usdt)

    adjustments = (
        await session.scalars(
            select(RebateAccrual).where(
                RebateAccrual.account_id == plan.account_id,
                RebateAccrual.correction_key.is_not(None),
            )
        )
    ).all()
    for row in adjustments:
        if row.query_from is None or row.query_to is None:
            raise ValueError("An existing correction has no source coverage.")
        if row.query_from <= plan.source_to and row.query_to >= plan.source_from:
            raise ValueError("A different correction already covers this period.")

    originals = (
        await session.scalars(
            select(RebateAccrual)
            .where(
                RebateAccrual.account_id == plan.account_id,
                RebateAccrual.correction_key.is_(None),
            )
            .order_by(RebateAccrual.id)
            .with_for_update()
        )
    ).all()
    covered = _covered_rows(originals, plan)
    if basis_sha256(covered) != plan.basis_sha256:
        raise ValueError("Accrual amounts changed since the reconciliation was prepared.")
    recorded_gross = sum((row.amount_usdt for row in covered), Decimal(0))
    recorded_net = sum((row.net_usdt for row in covered), Decimal(0))
    gross_delta = plan.expected_gross - recorded_gross
    net_delta = plan.expected_net - recorded_net
    if gross_delta < 0 or net_delta <= 0:
        raise ValueError("This operation only posts positive credits.")

    credit = RebateAccrual(
        account_id=plan.account_id,
        user_id=plan.user_id,
        accrual_date=plan.period_month,
        asset="USDT",
        amount_usdt=gross_delta,
        amount_native=gross_delta,
        commission_usdt=gross_delta - net_delta,
        net_usdt=net_delta,
        net_native=net_delta,
        fx_rate=Decimal(1),
        effective_rank=plan.effective_rank,
        fee_pct=None,
        state="available",
        available_at=now,
        expires_at=now + timedelta(days=30),
        finalized=True,
        raw_records_count=0,
        query_from=plan.source_from,
        query_to=plan.source_to,
        correction_key=plan.correction_key,
        adapter_notes=notes,
    )
    session.add(credit)
    await session.flush()
    session.add(
        AuditEvent(
            actor=actor,
            action="gate_reconciliation_credit",
            entity_type="rebate_accrual",
            entity_id=str(credit.id),
            amount_usdt=net_delta,
            old_value={
                "gross": str(recorded_gross),
                "net": str(recorded_net),
                "basis_ids": [row.id for row in covered],
                "basis_sha256": plan.basis_sha256,
            },
            new_value={
                "correction_key": plan.correction_key,
                "uid": plan.uid,
                "expected_gross": str(plan.expected_gross),
                "expected_net": str(plan.expected_net),
                "gross_delta": str(gross_delta),
                "net_delta": str(net_delta),
                "fee_delta": str(gross_delta - net_delta),
                "source_from": plan.source_from.isoformat(),
                "source_to": plan.source_to.isoformat(),
                "expires_at": credit.expires_at.isoformat(),
            },
        )
    )
    await session.flush()
    return CorrectionReceipt(credit.id, True, gross_delta, net_delta)
