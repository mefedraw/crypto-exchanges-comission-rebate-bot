"""Partner-pool reservations reuse the existing executor, never fake rebate rows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin import repository as admin_repo
from bot.botui import repository as repo
from bot.botui.service import MIN_PAYOUT_USDT, WITHDRAW_TOKEN_TTL, WithdrawBlocked
from bot.config import get_settings
from bot.db.models.identity import User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.partners import PartnerEarning
from bot.db.models.payouts import Payout
from bot.partners.repository import lock_account
from bot.partners.service import ZERO


@dataclass(frozen=True)
class PartnerPreview:
    earning_ids: list[int]
    amount_usdt: Decimal
    amount_native: Decimal
    asset: str
    wallet_id: int
    wallet_masked: str


async def _gate(session: AsyncSession, user_id: int, asset: str) -> WithdrawBlocked | None:
    if asset not in {"USDT", "MNT"}:
        return WithdrawBlocked("asset")
    if asset == "MNT" and not get_settings().mnt_enabled:
        return WithdrawBlocked("mnt_disabled")
    if await admin_repo.is_payouts_paused(session):
        return WithdrawBlocked("paused")
    if await repo.get_user_status(session, user_id) != "active":
        return WithdrawBlocked("blocked")
    if await repo.has_active_payout(session, user_id, asset=asset):
        return WithdrawBlocked("pending")
    return None


def _totals(rows: list[PartnerEarning]) -> tuple[Decimal, Decimal]:
    with localcontext() as ctx:
        ctx.prec = 80
        return (
            sum((r.amount_usdt for r in rows), ZERO),
            sum((r.amount_native for r in rows), ZERO),
        )


async def prepare(
    session: AsyncSession, user_id: int, asset: str
) -> PartnerPreview | WithdrawBlocked:
    blocked = await _gate(session, user_id, asset)
    if blocked is not None:
        return blocked
    wallet = await repo.get_active_wallet(session, user_id)
    if wallet is None:
        return WithdrawBlocked("no_wallet")
    rows = list(
        (
            await session.scalars(
                select(PartnerEarning)
                .where(
                    PartnerEarning.referrer_id == user_id,
                    PartnerEarning.asset == asset,
                    PartnerEarning.state == "available",
                    PartnerEarning.amount_usdt > ZERO,
                )
                .order_by(PartnerEarning.id)
            )
        ).all()
    )
    amount, native = _totals(rows)
    if amount < MIN_PAYOUT_USDT or native <= ZERO:
        return WithdrawBlocked("below_min")
    return PartnerPreview(
        [r.id for r in rows], amount, native, asset, wallet.id, wallet.address_masked
    )


async def issue_token(
    session: AsyncSession, user_id: int, preview: PartnerPreview, now: datetime
) -> uuid.UUID:
    token = uuid.uuid4()
    await repo.create_action_token(
        session,
        token=token,
        user_id=user_id,
        action_type="partner_withdraw",
        payload={
            "earning_ids": preview.earning_ids,
            "asset": preview.asset,
            "amount_usdt": str(preview.amount_usdt),
            "amount_native": str(preview.amount_native),
            "wallet_id": preview.wallet_id,
        },
        expires_at=now + WITHDRAW_TOKEN_TTL,
    )
    return token


async def confirm(
    session: AsyncSession, user_id: int, token: uuid.UUID, now: datetime
) -> Payout | WithdrawBlocked | None:
    await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
    # Leave the token unused on a pause/block, as the normal withdrawal flow does.
    if await admin_repo.is_payouts_paused(session):
        return WithdrawBlocked("paused")
    if await repo.get_user_status(session, user_id) != "active":
        return WithdrawBlocked("blocked")
    payload = await repo.consume_action_token(
        session, token, now, user_id=user_id, action_type="partner_withdraw"
    )
    if payload is None:
        return None
    asset = str(payload["asset"])
    blocked = await _gate(session, user_id, asset)
    if blocked is not None:
        await repo.release_action_token(session, token)
        return blocked
    wallet = await repo.get_active_wallet(session, user_id)
    if wallet is None or wallet.id != payload["wallet_id"]:
        return None
    await lock_account(session, user_id)
    ids = [int(value) for value in payload["earning_ids"]]
    if not ids or len(ids) != len(set(ids)):
        return None
    rows = list(
        (
            await session.scalars(
                select(PartnerEarning)
                .where(
                    PartnerEarning.id.in_(ids),
                    PartnerEarning.referrer_id == user_id,
                    PartnerEarning.asset == asset,
                )
                .order_by(PartnerEarning.id)
                .with_for_update()
            )
        ).all()
    )
    if len(rows) != len(ids) or any(r.state != "available" for r in rows):
        return None
    amount, native = _totals(rows)
    if (
        amount < MIN_PAYOUT_USDT
        or native <= ZERO
        or amount != Decimal(payload["amount_usdt"])
        or native != Decimal(payload["amount_native"])
    ):
        return None
    bounds = (
        await session.execute(
            select(
                func.min(RebateAccrual.accrual_date), func.max(RebateAccrual.accrual_date)
            ).where(RebateAccrual.id.in_([r.source_accrual_id for r in rows]))
        )
    ).one()
    payout = Payout(
        user_id=user_id,
        wallet_id=wallet.id,
        address_encrypted=wallet.address_encrypted,
        address_masked=wallet.address_masked,
        covered_from=bounds[0],
        covered_to=bounds[1],
        gross_usdt=amount,
        commission_usdt=ZERO,
        net_usdt=amount,
        asset=asset,
        net_native=native,
        purpose="partner",
        status="pending_admin" if asset == "MNT" else "reserved",
        idempotency_key=f"partner:{user_id}:{token}",
    )
    session.add(payout)
    await session.flush()
    for row in rows:
        row.state = "reserved"
        row.payout_id = payout.id
    await session.flush()
    await repo.write_audit(
        session,
        actor=str(user_id),
        action="partner_withdraw_reserved",
        entity_type="payout",
        entity_id=str(payout.id),
        new_value={"asset": asset, "amount_usdt": str(amount)},
    )
    return payout
