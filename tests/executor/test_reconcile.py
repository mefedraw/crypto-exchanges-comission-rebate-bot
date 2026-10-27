# tests/executor/test_reconcile.py
"""Startup reconciliation — the anti-double-spend proof.

The core invariant under test: a payout whose broadcast outcome was uncertain
(ChainError after the tx actually landed) must NEVER be re-broadcast. reconcile()
resolves it by checking the chain (by nonce), promotes it to 'sent', and lets
poll_confirmations finalize it — all without ever calling send_transfer a second time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.payouts import Payout
from bot.executor.chain import MockChainClient
from bot.executor.executor import PayoutExecutor
from bot.executor.scheduler import ExecutorScheduler
from bot.security.secrets import SecretCipher

pytestmark = pytest.mark.asyncio(loop_scope="session")

RECIPIENT = "0xrecipient000000000000000000000000000000"
FIXED_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sessionmaker(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real (non-nested-transaction) sessionmaker so the executor's own
    commits/rollbacks behave like production, while still cleaning the DB after
    each test.
    """
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    yield sm
    async with sm() as session:
        await session.execute(delete(RebateAccrual))
        await session.execute(delete(Payout))
        await session.execute(delete(ExchangeAccount))
        await session.execute(delete(Wallet))
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
    nonce: int | None = None,
    requested_at: datetime | None = None,
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
        nonce=nonce,
        idempotency_key=f"idem-{user_id}-{status}-{nonce}-{requested_at}",
    )
    if requested_at is not None:
        payout.requested_at = requested_at
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
    return RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=d,
        amount_usdt=amount,
        effective_rank=1,
        fee_pct=Decimal("0"),
        commission_usdt=Decimal("0"),
        net_usdt=amount,
        state=state,
        payout_id=payout_id,
        available_at=datetime(2026, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 10, tzinfo=UTC),
        raw_records_count=1,
        adapter_notes=None,
    )


async def _seed_reserved_payout_flow(
    sessionmaker: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    user_id: int,
    *,
    net_usdt: Decimal = Decimal("45"),
) -> int:
    """Seed a user+wallet+reserved payout+reserved accrual. Returns the payout id."""
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, user_id)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            user_id,
            wallet.id,
            "reserved",
            net_usdt=net_usdt,
            address_encrypted=addr_enc,
        )
        payout_id = payout.id
        session.add(_accrual(user_id, acc.id, date(2026, 7, 5), net_usdt, "reserved", payout_id))
        await session.commit()
    return payout_id


def _make_executor(
    sessionmaker: async_sessionmaker[AsyncSession],
    chain: MockChainClient,
    cipher: SecretCipher,
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


async def test_sent_but_unknown_never_double_sends(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """The anti-double-spend proof: broadcast raises ChainError but the tx DID land.
    process_next leaves the payout 'submitting' (does not fabricate 'sent'). reconcile()
    finds the landed tx by nonce, promotes to 'sent', and poll_confirmations finalizes to
    'paid' — all without a second call to send_transfer.
    """
    chain = MockChainClient(nonce=0, fail_send_but_land=True, default_confirmations=15)
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 1001)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        after_send = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert after_send is not None
        assert after_send.status == "submitting"
        assert after_send.nonce == 0

    assert len(chain.sent_calls) == 1

    await executor.reconcile()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "paid"
        assert refreshed.confirmed_at == FIXED_NOW
        assert refreshed.paid_at == FIXED_NOW

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 1001))
        ).all()
        assert len(accruals) == 1
        assert accruals[0].state == "paid"

    # THE anti-double-spend assertion: exactly one broadcast ever happened.
    assert len(chain.sent_calls) == 1


async def test_hash_persisted_before_broadcast(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """#3: on_signed persists the deterministic hash while 'submitting', BEFORE the
    broadcast. So even when broadcast raises (fail_send_but_land) the payout already
    carries its txhash — reconcile can resolve it from the DB alone, not from this
    process's in-memory nonce->hash map (which a restart would lose)."""
    chain = MockChainClient(nonce=0, fail_send_but_land=True, default_confirmations=15)
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 1004)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()  # broadcast raises -> left submitting

    async with sessionmaker() as session:
        payout = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert payout is not None
        assert payout.status == "submitting"
        assert payout.nonce is not None
        # the hash is already durable, persisted before the (failed) broadcast
        assert payout.txhash == MockChainClient._hash_for(payout.nonce)


async def test_submitting_not_landed_requeued_to_reserved(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """Broadcast fails and nothing landed: reconcile must NOT fabricate 'paid'/'sent'.
    It now REQUEUES the payout to 'reserved' (clearing nonce+txhash) so process_next
    re-sends it cleanly — instead of leaving it stuck 'submitting', which blocks the
    single-flight queue forever. Safe: is_nonce_used proved the tx never landed.
    """
    chain = MockChainClient(nonce=0, fail_send_no_land=True)
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 1002)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        after_send = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert after_send is not None
        assert after_send.status == "submitting"

    assert await chain.find_tx_by_nonce("0xwallet", 0) is None

    await executor.reconcile()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "reserved"  # requeued, no longer stuck submitting
        assert refreshed.nonce is None  # cleared so process_next recomputes a fresh nonce
        assert refreshed.txhash is None

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 1002))
        ).all()
        assert len(accruals) == 1
        assert accruals[0].state == "reserved"

    # reconcile itself never re-broadcasts — the resend is process_next's job (below test).
    assert len(chain.sent_calls) == 1


async def test_requeued_not_landed_resends_on_next_process_next(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """End-to-end auto-recovery (no manual DB surgery): a never-landed 'submitting' is
    requeued by reconcile, then RE-SENT by the following process_next. The mock is
    flipped to succeed on the resend to model the transient cause clearing (e.g. the
    wallet being funded)."""
    chain = MockChainClient(nonce=0, fail_send_no_land=True)
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 1006)
    executor = _make_executor(sessionmaker, chain, cipher)

    await executor.process_next()  # -> submitting (broadcast failed, nothing landed)
    await executor.reconcile()  # -> reserved (requeued)

    async with sessionmaker() as session:
        p = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert p is not None and p.status == "reserved"

    chain._fail_send_no_land = False  # transient cause cleared
    await executor.process_next()  # re-sends cleanly

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.nonce == 0  # recomputed max(onchain 0, no prior db nonce) == 0

    assert len(chain.sent_calls) == 2  # original attempt + the successful resend


async def test_scheduler_loop_heals_submitting_without_restart(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """Fix 1: a broadcast-uncertain payout (tx landed, broadcast raised) is left
    'submitting' by process_next, then healed by the scheduler's PER-TICK reconcile —
    no process restart. The running loop drives it submitting -> sent -> paid, and it
    is never re-broadcast.
    """
    chain = MockChainClient(nonce=0, fail_send_but_land=True, default_confirmations=15)
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 1005)
    executor = _make_executor(sessionmaker, chain, cipher)

    async def _sleep(_seconds: float) -> None:
        return None

    scheduler = ExecutorScheduler(executor, sleep=_sleep, now=_fixed_now, interval_seconds=1.0)
    # tick 1: reconcile (nothing in flight) -> process_next sends, broadcast raises,
    #         payout left 'submitting'. tick 2: reconcile finds the landed nonce ->
    #         sent -> paid. No restart, no manual reconcile() call.
    await scheduler.run_forever(max_cycles=2)

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "paid"

    assert len(chain.sent_calls) == 1  # healed, never re-broadcast


async def test_reconcile_finalizes_sent(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """A payout already 'sent' with >= CONFIRMATIONS is driven to 'paid' by reconcile
    (it reuses the same finalize logic as poll_confirmations)."""
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 1003)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        sent = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert sent is not None
        assert sent.status == "sent"

    await executor.reconcile()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "paid"

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 1003))
        ).all()
        assert accruals[0].state == "paid"

    assert len(chain.sent_calls) == 1
