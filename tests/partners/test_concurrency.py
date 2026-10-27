from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.admin import repository as admin_repo
from bot.botui import service as rebate_service
from bot.botui.service import WithdrawBlocked, set_wallet
from bot.db.models.identity import ExchangeAccount
from bot.db.models.partners import PartnerEarning
from bot.db.models.payouts import Payout
from bot.executor.chain import MockChainClient
from bot.executor.executor import PayoutExecutor
from bot.partners import service, withdrawal
from bot.security.secrets import SecretCipher
from tests.partners.test_service import NOW, PARENT, START, historical, new_source, seed
from tests.partners.test_withdrawal import token_for

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_concurrent_source_retries_allocate_once(committed_engine):
    sm = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with sm() as session:
        accounts = await seed(session)
        account_id = accounts[0].id
        await session.commit()

    async def accrue():
        async with sm() as session:
            account = await session.get(ExchangeAccount, account_id)
            await new_source(session, account)
            await session.commit()

    await asyncio.wait_for(asyncio.gather(*(accrue() for _ in range(3))), timeout=20)
    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(PartnerEarning)) == 1
        assert (await service.summary(session, PARENT)).balances["USDT"].available == 2


@pytest.mark.parametrize("asset", ["USDT", "MNT"])
async def test_parallel_confirmations_one_payout_and_full_mocked_chain_lifecycle(
    committed_engine, monkeypatch, asset
):
    if asset == "MNT":
        from tests.conftest import make_settings

        monkeypatch.setattr(
            withdrawal,
            "get_settings",
            lambda: make_settings(MANTLE_RPC_URL="https://rpc.example.com"),
        )
    sm = async_sessionmaker(committed_engine, expire_on_commit=False)
    cipher = SecretCipher(Fernet.generate_key().decode())
    async with sm() as session:
        accounts = await seed(session)
        await new_source(session, accounts[0], "1000.12345", asset=asset)
        await set_wallet(session, cipher, PARENT, "0x" + "1" * 40)
        one, two = await token_for(session, asset=asset), await token_for(session, asset=asset)
        await session.commit()

    async def confirm(token):
        async with sm() as session:
            result = await withdrawal.confirm(session, PARENT, token, NOW)
            await session.commit()
            return result

    results = await asyncio.wait_for(asyncio.gather(confirm(one), confirm(two)), timeout=20)
    assert sum(isinstance(result, Payout) for result in results) == 1
    assert sum(isinstance(result, WithdrawBlocked) for result in results) == 1
    if asset == "MNT":
        async with sm() as session:
            payout = await session.scalar(select(Payout))
            assert payout.status == "pending_admin"
            assert await admin_repo.release_payout(session, payout.id, PARENT)
            await session.commit()
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    executor = PayoutExecutor(
        sm,
        chain,
        cipher,
        now=lambda: NOW,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        mantle_chain=chain if asset == "MNT" else None,
    )
    await executor.process_next()
    await executor.poll_confirmations()
    await executor.poll_confirmations()
    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(Payout)) == 1
        payout = await session.scalar(select(Payout))
        assert payout.status == "paid" and payout.purpose == "partner"
        summary = await service.summary(session, PARENT)
        assert summary.balances[asset].paid == Decimal("20.002469")
        assert summary.balances[asset].available == 0
        calls = chain.native_sent_calls if asset == "MNT" else chain.sent_calls
        assert len(calls) == 1 and calls[0]["amount_wei"] == 20002469000000000000


async def test_rebate_and_partner_confirmations_share_one_active_payout_guard(committed_engine):
    sm = async_sessionmaker(committed_engine, expire_on_commit=False)
    cipher = SecretCipher(Fernet.generate_key().decode())
    async with sm() as session:
        accounts = await seed(session)
        await new_source(session, accounts[0], "1000")
        own = ExchangeAccount(
            user_id=PARENT, exchange="gate", uid="parent-own", status="active", accrual_since=START
        )
        session.add(own)
        await session.flush()
        session.add(historical(own, Decimal("20")))
        await set_wallet(session, cipher, PARENT, "0x" + "1" * 40)
        partner_token = await token_for(session)
        preview = await rebate_service.prepare_withdrawal(session, PARENT, NOW, NOW.date())
        assert isinstance(preview, rebate_service.WithdrawPreview)
        rebate_token = await rebate_service.issue_withdraw_token(session, PARENT, preview, NOW)
        await session.commit()

    async def confirm(token, handler):
        async with sm() as session:
            result = await handler(session, PARENT, token, NOW)
            await session.commit()
            return result

    results = await asyncio.wait_for(
        asyncio.gather(
            confirm(partner_token, withdrawal.confirm),
            confirm(rebate_token, rebate_service.confirm_withdrawal),
        ),
        timeout=20,
    )
    assert sum(isinstance(result, Payout) for result in results) == 1
    assert sum(isinstance(result, WithdrawBlocked) for result in results) == 1
    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(Payout)) == 1
