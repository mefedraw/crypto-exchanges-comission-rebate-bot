from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from bot.admin import repository as admin_repo
from bot.botui import repository as cabinet_repo
from bot.botui.service import WithdrawBlocked, confirm_withdrawal
from bot.db.models.identity import User, Wallet
from bot.db.models.ops import ActionToken
from bot.db.models.payouts import Payout
from bot.executor import repository as executor_repo
from bot.partners import service, withdrawal
from tests.partners.test_service import NOW, PARENT, earning_for, new_source, seed

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def ready(session, *, asset="USDT"):
    session.sync_session.expire_on_commit = False
    accounts = await seed(session)
    row = await new_source(
        session, accounts[0], "1000", asset=asset, fx="2" if asset == "MNT" else "1"
    )
    session.add(
        Wallet(
            user_id=PARENT,
            address_encrypted=b"encrypted",
            address_hash=b"hash",
            address_masked="0x1234...abcd",
        )
    )
    await session.flush()
    return accounts, row


async def token_for(session, *, asset="USDT"):
    preview = await withdrawal.prepare(session, PARENT, asset)
    assert isinstance(preview, withdrawal.PartnerPreview)
    return await withdrawal.issue_token(session, PARENT, preview, NOW)


async def test_partner_withdraws_without_own_uid_or_rebate_rank_and_no_second_fee(db_session):
    _, source = await ready(db_session)
    token = await token_for(db_session)
    payout = await withdrawal.confirm(db_session, PARENT, token, NOW)
    assert isinstance(payout, Payout)
    assert payout.purpose == "partner" and payout.asset == "USDT"
    assert payout.net_usdt == 20 and payout.gross_usdt == 20 and payout.commission_usdt == 0
    assert source.state == "available" and source.payout_id is None and source.net_usdt == 900
    summary = await service.summary(db_session, PARENT)
    assert summary.balances["USDT"].available == 0 and summary.balances["USDT"].reserved == 20
    assert await withdrawal.confirm(db_session, PARENT, token, NOW) is None
    assert await db_session.scalar(select(func.count()).select_from(Payout)) == 1


async def test_token_owner_and_action_cannot_be_substituted(db_session):
    await ready(db_session)
    db_session.add(User(id=84999, role="user", status="active"))
    await db_session.flush()
    token = await token_for(db_session)
    assert await withdrawal.confirm(db_session, 84999, token, NOW) is None
    assert await confirm_withdrawal(db_session, PARENT, token, NOW) is None
    assert (await db_session.get(ActionToken, token)).state == "issued"
    payout = await withdrawal.confirm(db_session, PARENT, token, NOW)
    assert isinstance(payout, Payout)


@pytest.mark.parametrize("gate", ["paused", "blocked", "manual_review"])
async def test_security_gates_rechecked_without_consuming_token(db_session, gate):
    await ready(db_session)
    token = await token_for(db_session)
    if gate == "paused":
        await admin_repo.set_flag(
            db_session, admin_repo.PAYOUTS_PAUSED_KEY, {"paused": True}, PARENT
        )
    else:
        user = await db_session.get(User, PARENT)
        user.status = gate
        await db_session.flush()
    result = await withdrawal.confirm(db_session, PARENT, token, NOW)
    assert isinstance(result, WithdrawBlocked)
    assert (await db_session.get(ActionToken, token)).state == "issued"
    assert await db_session.scalar(select(func.count()).select_from(Payout)) == 0


async def test_different_live_tokens_cannot_reserve_twice(db_session):
    await ready(db_session)
    one, two = await token_for(db_session), await token_for(db_session)
    assert isinstance(await withdrawal.confirm(db_session, PARENT, one, NOW), Payout)
    result = await withdrawal.confirm(db_session, PARENT, two, NOW)
    assert isinstance(result, WithdrawBlocked) and result.reason == "pending"
    assert (await db_session.get(ActionToken, two)).state == "issued"


async def test_expiry_wallet_change_and_payload_drift_fail_closed(db_session):
    await ready(db_session)
    expired = await token_for(db_session)
    assert await withdrawal.confirm(db_session, PARENT, expired, NOW + timedelta(minutes=6)) is None
    token = await token_for(db_session)
    action = await db_session.get(ActionToken, token)
    action.payload = {**action.payload, "amount_usdt": "999"}
    await db_session.flush()
    assert await withdrawal.confirm(db_session, PARENT, token, NOW) is None
    token = await token_for(db_session)
    await cabinet_repo.replace_wallet(
        db_session,
        PARENT,
        address_encrypted=b"new",
        address_hash=b"new-hash",
        address_masked="0x9999...ffff",
    )
    assert await withdrawal.confirm(db_session, PARENT, token, NOW) is None
    assert await db_session.scalar(select(func.count()).select_from(Payout)) == 0


async def test_executor_failure_returns_partner_money_without_touching_source(db_session):
    _, source = await ready(db_session)
    payout = await withdrawal.confirm(db_session, PARENT, await token_for(db_session), NOW)
    await executor_repo.finalize_failed(db_session, payout.id, "user_blocked")
    assert (await earning_for(db_session, source)).state == "available"
    assert (await service.summary(db_session, PARENT)).balances["USDT"].available == 20
    assert source.state == "available" and source.net_usdt == 900


async def test_executor_confirmation_is_final_and_does_not_apply_rebate_cooldown(db_session):
    _, source = await ready(db_session)
    payout = await withdrawal.confirm(db_session, PARENT, await token_for(db_session), NOW)
    await executor_repo.mark_submitting(db_session, payout.id, 0)
    await executor_repo.mark_sent(db_session, payout.id, "0xtest", NOW)
    await executor_repo.finalize_paid(db_session, payout.id, NOW)
    await executor_repo.finalize_paid(db_session, payout.id, NOW)
    await executor_repo.finalize_failed(db_session, payout.id, "late_failure")
    earning = await earning_for(db_session, source)
    assert earning.state == "paid" and payout.status == "paid"
    summary = await service.summary(db_session, PARENT)
    assert summary.balances["USDT"].paid == 20 and summary.balances["USDT"].available == 0
    assert await cabinet_repo.last_success_withdrawal_at(db_session, PARENT, "USDT") is None


async def test_mnt_keeps_native_amount_and_admin_gate(db_session, monkeypatch):
    from tests.conftest import make_settings

    monkeypatch.setattr(
        withdrawal, "get_settings", lambda: make_settings(MANTLE_RPC_URL="https://rpc.example.com")
    )
    _, source = await ready(db_session, asset="MNT")
    token = await token_for(db_session, asset="MNT")
    payout = await withdrawal.confirm(db_session, PARENT, token, NOW)
    assert payout.asset == "MNT" and payout.net_native == 10 and payout.net_usdt == 20
    assert payout.status == "pending_admin"
    assert await admin_repo.reject_payout(db_session, payout.id)
    earning = await earning_for(db_session, source)
    assert earning.state == "available" and earning.payout_id is None
    assert (await service.summary(db_session, PARENT)).balances["USDT"].available == 0


async def test_payout_fee_discount_cannot_spend_partner_backing(db_session):
    accounts, source = await ready(db_session)
    db_session.add(
        Wallet(
            user_id=source.user_id,
            address_encrypted=b"enc",
            address_hash=b"child",
            address_masked="child",
        )
    )
    await db_session.flush()
    wallet = await cabinet_repo.get_active_wallet(db_session, source.user_id)
    payout = await cabinet_repo.reserve_accruals_and_create_payout(
        db_session,
        source.user_id,
        [source.id],
        wallet=wallet,
        gross=source.amount_usdt,
        commission=source.commission_usdt,
        net=source.net_usdt,
        net_native=source.net_native,
        idempotency_key="protected",
        initial_status="pending_admin",
    )
    assert not await admin_repo.reduce_payout_commission(
        db_session, payout.id, Decimal(0), PARENT, "discount"
    )
    assert payout.net_usdt == 900 and (await earning_for(db_session, source)).amount_usdt == 20


async def test_standard_token_cannot_be_used_as_partner_token(db_session):
    await ready(db_session)
    token = uuid.uuid4()
    await cabinet_repo.create_action_token(
        db_session,
        token=token,
        user_id=PARENT,
        action_type="withdraw",
        payload={},
        expires_at=NOW + timedelta(minutes=5),
    )
    assert await withdrawal.confirm(db_session, PARENT, token, NOW) is None
    assert (await db_session.get(ActionToken, token)).state == "issued"
