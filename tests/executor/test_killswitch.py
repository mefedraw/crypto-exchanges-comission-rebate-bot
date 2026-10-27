# tests/executor/test_killswitch.py
"""Kill-switch enforcement in the executor: a paused ``payouts_paused`` flag must
stop ALL sends — a ready `reserved` payout stays `reserved` and nothing reaches
the chain client. Un-pausing resumes the normal flow (§ Task 6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.admin import repository as admin_repo
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import SystemFlag
from bot.db.models.payouts import Payout
from bot.executor.chain import MockChainClient
from bot.executor.executor import PayoutExecutor
from bot.security.secrets import SecretCipher

pytestmark = pytest.mark.asyncio(loop_scope="session")

RECIPIENT = "0xrecipient000000000000000000000000000000"
FIXED_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sessionmaker(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real (non-nested-transaction) sessionmaker, matching test_process_next.py,
    so the executor's own commits behave like production."""
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    yield sm
    async with sm() as session:
        await session.execute(delete(RebateAccrual))
        await session.execute(delete(Payout))
        await session.execute(delete(ExchangeAccount))
        await session.execute(delete(Wallet))
        await session.execute(delete(SystemFlag))  # clear kill-switch flag between tests
        await session.execute(delete(User))
        await session.commit()


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher(Fernet.generate_key().decode())


def _fixed_now() -> datetime:
    return FIXED_NOW


async def _seed_user_wallet(
    session: AsyncSession, user_id: int, uid: str = "U"
) -> tuple[ExchangeAccount, Wallet]:
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
        uid=f"{uid}{user_id}",
        status="active",
        accrual_since=date(2026, 7, 1),
    )
    session.add(acc)
    await session.flush()
    return acc, wallet


async def _seed_payout(
    session: AsyncSession,
    user_id: int,
    wallet_id: int,
    status: str,
    *,
    net_usdt: Decimal,
    address_encrypted: bytes,
) -> Payout:
    payout = Payout(
        user_id=user_id,
        wallet_id=wallet_id,
        address_encrypted=address_encrypted,
        address_masked="0x..abcd",
        covered_from=date(2026, 7, 1),
        covered_to=date(2026, 7, 31),
        gross_usdt=net_usdt,
        commission_usdt=Decimal("0"),
        net_usdt=net_usdt,
        status=status,
        idempotency_key=f"idem-{user_id}-{status}-killswitch",
    )
    session.add(payout)
    await session.flush()
    return payout


def _accrual(
    user_id: int, account_id: int, d: date, amount: Decimal, payout_id: int | None
) -> RebateAccrual:
    return RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=d,
        amount_usdt=amount,
        effective_rank=1,
        fee_pct=Decimal("0"),
        commission_usdt=Decimal("0"),
        net_usdt=amount,
        state="reserved",
        payout_id=payout_id,
        available_at=datetime(2026, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 10, tzinfo=UTC),
        raw_records_count=1,
        adapter_notes=None,
    )


def _executor(
    sessionmaker: async_sessionmaker[AsyncSession], chain: MockChainClient, cipher: SecretCipher
) -> PayoutExecutor:
    return PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )


async def test_paused_kill_switch_blocks_send_then_unpause_resumes(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=5)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 901)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session, 901, wallet.id, "reserved", net_usdt=Decimal("45"), address_encrypted=addr_enc
        )
        payout_id = payout.id
        session.add(_accrual(901, acc.id, date(2026, 7, 5), Decimal("45"), payout_id))
        await admin_repo.set_flag(session, admin_repo.PAYOUTS_PAUSED_KEY, {"paused": True}, by=901)
        await session.commit()

    executor = _executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "reserved"  # untouched: nothing reserved/claimed/sent
    assert chain.sent_calls == []

    # Un-pause -> the same payout is now free to be picked up and sent.
    async with sessionmaker() as session:
        await admin_repo.set_flag(session, admin_repo.PAYOUTS_PAUSED_KEY, {"paused": False}, by=901)
        await session.commit()

    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
    assert len(chain.sent_calls) == 1


async def test_blocked_user_reserved_payout_is_failed_not_sent(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """A payout reserved BEFORE an admin blocked the user must not be sent. The
    executor fails it (accruals returned to 'available') rather than skipping it —
    skipping would leave it as the oldest 'reserved' row and starve everyone else."""
    chain = MockChainClient(nonce=5)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 902)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session, 902, wallet.id, "reserved", net_usdt=Decimal("45"), address_encrypted=addr_enc
        )
        payout_id = payout.id
        session.add(_accrual(902, acc.id, date(2026, 7, 5), Decimal("45"), payout_id))
        # admin blocks the owner while the payout sits reserved
        await admin_repo.set_user_status(session, 902, "blocked")
        await session.commit()

    executor = _executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.failed_reason == "user_blocked"
        acc_row = await session.scalar(
            select(RebateAccrual).where(RebateAccrual.payout_id.is_(None))
        )
        assert acc_row is not None and acc_row.state == "available"  # money preserved
    assert chain.sent_calls == []  # nothing reached the chain
