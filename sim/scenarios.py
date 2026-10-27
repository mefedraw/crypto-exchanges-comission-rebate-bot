"""Scenario injectors for the life-sim: one-shot admin/edge events fired on scheduled
sim-days. Each returns an outcome dict the report prints and invariants check. All money
math and state transitions go through the REAL bot repositories — these only orchestrate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.admin import repository as admin_repo
from bot.botui import repository as botui_repo
from bot.botui import service as botui
from bot.db.models.payouts import Payout
from bot.executor.chain import MockChainClient
from bot.security.secrets import SecretCipher


async def event_reassign_uid(
    sm: async_sessionmaker[AsyncSession],
    exchange: str,
    uid: str,
    old_user_id: int,
    new_user_id: int,
) -> dict:
    """Admin resolves a UID dispute: move (exchange, uid) ownership to new_user_id."""
    async with sm() as s:
        ok = await admin_repo.reassign_uid(s, exchange, uid, new_user_id)
        holder = await botui_repo.uid_holder(s, exchange, uid)
        await s.commit()
    return {
        "exchange": exchange,
        "uid": uid,
        "from": old_user_id,
        "to": new_user_id,
        "applied": ok,
        "new_holder": None if holder is None else holder[0],
    }


async def event_block_uid(sm: async_sessionmaker[AsyncSession], exchange: str, uid: str) -> dict:
    """Admin blocks an active (exchange, uid); future accruals for it stop."""
    async with sm() as s:
        ok = await admin_repo.block_uid(s, exchange, uid)
        holder = await botui_repo.uid_holder(s, exchange, uid)
        await s.commit()
    return {
        "exchange": exchange,
        "uid": uid,
        "applied": ok,
        "status_now": None if holder is None else holder[1],
    }


async def event_reduce_commission(sm: async_sessionmaker[AsyncSession], admin_id: int) -> dict:
    """Halve the commission on the oldest pending_admin USDT payout (only-down)."""
    async with sm() as s:
        rows = await admin_repo.list_pending_admin(s)
        target = next((p for p in rows if p.asset == "USDT"), None)
        if target is None:
            return {"applied": False, "reason": "no pending_admin USDT payout"}
        pid = target.id
        before_net = target.net_usdt
        before_comm = target.commission_usdt
        new_comm = (before_comm / 2).quantize(Decimal("0.00000001"))
        ok = await admin_repo.reduce_payout_commission(s, pid, new_comm, admin_id, "sim reduce")
        after = await s.get(Payout, pid)
        after_net = after.net_usdt if after is not None else before_net
        await s.commit()
    return {
        "applied": ok,
        "payout_id": pid,
        "comm_before": before_comm,
        "comm_after": new_comm,
        "net_before": before_net,
        "net_after": after_net,
    }


async def event_new_user_mid_sim(
    sm: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    user_id: int,
    username: str | None,
    wallet: str,
    exchange: str,
    uid: str,
    accrual_since: date,
) -> dict:
    """A brand-new user joins mid-sim with one account (accrual_since honored)."""
    async with sm() as s:
        await botui_repo.upsert_user(s, user_id, username=username)
        await botui.set_wallet(s, cipher, user_id, wallet)
        acc = await botui_repo.add_account(s, user_id, exchange, uid, accrual_since)
        await s.commit()
    return {
        "user_id": user_id,
        "exchange": exchange,
        "uid": uid,
        "added": acc is not None,
        "accrual_since": accrual_since.isoformat(),
    }


def revert_next_paid_tx(mock: MockChainClient, nonce: int) -> dict:
    """Force the mock tx at ``nonce`` to report on-chain FAILURE, so the executor's
    poll_confirmations finalizes that payout as 'failed' (accruals return to available).
    Call AFTER the payout was broadcast (its nonce recorded). No-op if nonce unknown."""
    try:
        mock.set_status(nonce, "failed")
        return {"applied": True, "nonce": nonce}
    except KeyError:
        return {"applied": False, "nonce": nonce, "reason": "nonce not broadcast"}


async def event_uid_collision(
    sm: async_sessionmaker[AsyncSession], exchange: str, uid: str, other_user_id: int
) -> dict:
    """A second user tries to claim a uid already held by someone else -> first-come."""
    async with sm() as s:
        acc = await botui_repo.add_account(s, other_user_id, exchange, uid, date(2026, 1, 1))
        holder = await botui_repo.uid_holder(s, exchange, uid)
        await s.commit()
    # add_account returns None on a cross-user collision.
    return {
        "exchange": exchange,
        "uid": uid,
        "claimant": other_user_id,
        "rejected": acc is None,
        "held_by": None if holder is None else holder[0],
    }
