# tests/executor/test_invariants.py
"""Regression tests for two money-safety invariants (plan §6.2):

1. `expire_aged` only ages out `available` accruals by `expires_at` — a `reserved`
   accrual (money already earmarked for a payout, whether that payout is still
   `reserved` or has been bumped to `pending_admin`) must NEVER be silently expired
   out from under an in-flight payout just because its clock ran out.
2. The executor leaves an audit trail: `payout_sent` / `payout_paid` on the happy
   path, `payout_failed` on a reverted tx.
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

from bot.accrual.repository import expire_aged
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent
from bot.db.models.payouts import Payout
from bot.executor.chain import MockChainClient
from bot.executor.executor import PayoutExecutor
from bot.security.secrets import SecretCipher

pytestmark = pytest.mark.asyncio(loop_scope="session")

RECIPIENT = "0xrecipient000000000000000000000000000000"
FIXED_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
PAST_EXPIRY = datetime(2026, 6, 1, tzinfo=UTC)  # well before FIXED_NOW
AFTER_EXPIRY = datetime(2026, 7, 1, tzinfo=UTC)  # after PAST_EXPIRY, still before FIXED_NOW


@pytest_asyncio.fixture
async def sessionmaker(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real (non-nested-transaction) sessionmaker so the executor's own
    commits/rollbacks behave like production, while still cleaning the DB after
    each test.
    """
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    yield sm
    async with sm() as session:
        await session.execute(delete(AuditEvent))
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
        idempotency_key=f"idem-{user_id}-{status}-{nonce}",
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
    *,
    expires_at: datetime,
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
        available_at=datetime(2026, 5, 12, tzinfo=UTC),
        expires_at=expires_at,
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
        session.add(
            _accrual(
                user_id,
                acc.id,
                date(2026, 7, 5),
                net_usdt,
                "reserved",
                payout_id,
                expires_at=datetime(2026, 8, 10, tzinfo=UTC),
            )
        )
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


# --------------------------------------------------------------------------
# Expiry pause: `expire_aged` must skip reserved money (§6.2)
# --------------------------------------------------------------------------


async def test_expire_aged_skips_reserved_but_expires_available(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """A `reserved` accrual (earmarked for an in-flight `reserved` payout) survives
    `expire_aged` even though its `expires_at` is in the past — only `available`
    rows age out. Proven both negatively (reserved survives) and positively (an
    `available` sibling with the same past expiry DOES flip to `expired`)."""
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 950)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            950,
            wallet.id,
            "reserved",
            net_usdt=Decimal("45"),
            address_encrypted=addr_enc,
        )
        reserved_accrual = _accrual(
            950,
            acc.id,
            date(2026, 6, 1),
            Decimal("45"),
            "reserved",
            payout.id,
            expires_at=PAST_EXPIRY,
        )
        available_accrual = _accrual(
            950,
            acc.id,
            date(2026, 6, 2),
            Decimal("7"),
            "available",
            None,
            expires_at=PAST_EXPIRY,
        )
        session.add_all([reserved_accrual, available_accrual])
        await session.commit()
        reserved_id = reserved_accrual.id
        available_id = available_accrual.id

    async with sessionmaker() as session:
        rowcount = await expire_aged(session, AFTER_EXPIRY)
        await session.commit()

    assert rowcount == 1  # only the available row was touched

    async with sessionmaker() as session:
        reserved_after = await session.scalar(
            select(RebateAccrual).where(RebateAccrual.id == reserved_id)
        )
        available_after = await session.scalar(
            select(RebateAccrual).where(RebateAccrual.id == available_id)
        )
        assert reserved_after is not None
        assert reserved_after.state == "reserved"
        assert available_after is not None
        assert available_after.state == "expired"


async def test_expire_aged_skips_reserved_accruals_of_pending_admin_payout(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """Same invariant, but for a payout that was bumped to `pending_admin` (over the
    auto-payout limit) rather than staying `reserved` — its accruals are still
    `state='reserved'` and must not be aged out from under the pending admin review."""
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 951)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            951,
            wallet.id,
            "pending_admin",
            net_usdt=Decimal("250"),
            address_encrypted=addr_enc,
        )
        reserved_accrual = _accrual(
            951,
            acc.id,
            date(2026, 6, 1),
            Decimal("250"),
            "reserved",
            payout.id,
            expires_at=PAST_EXPIRY,
        )
        session.add(reserved_accrual)
        await session.commit()
        reserved_id = reserved_accrual.id

    async with sessionmaker() as session:
        await expire_aged(session, AFTER_EXPIRY)
        await session.commit()

    async with sessionmaker() as session:
        reserved_after = await session.scalar(
            select(RebateAccrual).where(RebateAccrual.id == reserved_id)
        )
        assert reserved_after is not None
        assert reserved_after.state == "reserved"


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


async def test_happy_path_writes_sent_and_paid_audit_events(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 960)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()
    await executor.poll_confirmations()

    async with sessionmaker() as session:
        payout = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert payout is not None
        assert payout.status == "paid"

        events = (
            await session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_type == "payout",
                    AuditEvent.entity_id == str(payout_id),
                    AuditEvent.action.in_(("payout_sent", "payout_paid")),
                )
                .order_by(AuditEvent.id)
            )
        ).all()

    actions = [e.action for e in events]
    assert "payout_sent" in actions
    assert "payout_paid" in actions
    assert all(e.actor == "executor" for e in events)


async def test_reverted_tx_writes_failed_audit_event(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 961)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        sent = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert sent is not None
        nonce = sent.nonce
        assert nonce is not None

    chain.set_status(nonce, "failed")
    await executor.poll_confirmations()

    async with sessionmaker() as session:
        payout = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert payout is not None
        assert payout.status == "failed"

        failed_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "payout",
                    AuditEvent.entity_id == str(payout_id),
                    AuditEvent.action == "payout_failed",
                )
            )
        ).all()

    assert len(failed_events) == 1
    assert failed_events[0].actor == "executor"
