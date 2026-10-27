# tests/executor/test_confirmations.py
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.botui import repository as botui_repo
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import Notification
from bot.db.models.payouts import Payout
from bot.executor.chain import GasQuote, MockChainClient, usdt_to_wei
from bot.executor.executor import PayoutExecutor
from bot.notifications import repository as notif_repo
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
        await session.execute(delete(Notification))
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
    asset: str = "USDT",
    net_native: Decimal | None = None,
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
        asset=asset,
        net_native=net_native if net_native is not None else net_usdt,
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
    asset: str = "USDT",
    net_native: Decimal | None = None,
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
            asset=asset,
            net_native=net_native,
        )
        payout_id = payout.id
        session.add(_accrual(user_id, acc.id, date(2026, 7, 5), net_usdt, "reserved", payout_id))
        await session.commit()
    return payout_id


def _make_executor(
    sessionmaker: async_sessionmaker[AsyncSession],
    chain: MockChainClient,
    cipher: SecretCipher,
    *,
    developer_id: int | None = None,
    mantle_chain: MockChainClient | None = None,
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
        developer_id=developer_id,
        mantle_chain=mantle_chain,
    )


async def _notifications(
    sessionmaker: async_sessionmaker[AsyncSession], recipient: int
) -> list[Notification]:
    async with sessionmaker() as session:
        return list(
            (
                await session.scalars(
                    select(Notification).where(Notification.recipient_chat_id == recipient)
                )
            ).all()
        )


async def test_paid_enqueues_user_arrival_notification(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    await _seed_reserved_payout_flow(sessionmaker, cipher, 910, net_usdt=Decimal("14.64"))

    executor = _make_executor(sessionmaker, chain, cipher, developer_id=42)
    await executor.process_next()
    await executor.poll_confirmations()

    notes = await _notifications(sessionmaker, 910)
    assert len(notes) == 1
    assert notes[0].kind == notif_repo.PAYOUT_PAID
    assert Decimal(notes[0].payload["net_usdt"]) == Decimal("14.64")
    assert notes[0].payload["txhash"]  # the mock tx hash is recorded for the BscScan link


async def test_mnt_paid_enqueues_notification_with_native_amount_and_asset(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """FINDING 2: a paid MNT payout's notification payload must carry the native MNT
    amount + asset="MNT" — never a USDT-equivalent number mislabeled as USDT."""
    bsc_chain = MockChainClient(nonce=0)
    mantle_chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(
        sessionmaker,
        cipher,
        912,
        net_usdt=Decimal("60"),
        asset="MNT",
        net_native=Decimal("100"),
    )

    executor = _make_executor(sessionmaker, bsc_chain, cipher, mantle_chain=mantle_chain)
    await executor.process_next()
    await executor.poll_confirmations()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "paid"

    notes = await _notifications(sessionmaker, 912)
    assert len(notes) == 1
    assert notes[0].kind == notif_repo.PAYOUT_PAID
    assert notes[0].payload["asset"] == "MNT"
    assert Decimal(notes[0].payload["net_native"]) == Decimal("100")


async def test_gas_deducted_recorded_and_notified(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    # The user bears the gas: the on-chain amount is net - gas, the payout records the
    # gas fields, and the arrival notification shows the net-of-gas amount (#2).
    gas = GasQuote(
        gas_bnb=Decimal("0.00012"), gas_usdt=Decimal("0.072"), bnb_usdt_rate=Decimal("600")
    )
    chain = MockChainClient(
        nonce=0, default_confirmations=15, default_status="success", gas_quote=gas
    )
    await _seed_reserved_payout_flow(sessionmaker, cipher, 930, net_usdt=Decimal("14.64"))

    executor = _make_executor(sessionmaker, chain, cipher, developer_id=42)
    await executor.process_next()
    await executor.poll_confirmations()

    # amount actually broadcast = usdt_to_wei(net - gas)
    assert chain.sent_calls[0]["amount_wei"] == usdt_to_wei(Decimal("14.64") - Decimal("0.072"))
    async with sessionmaker() as session:
        p = await session.scalar(select(Payout).where(Payout.user_id == 930))
        assert p is not None
        assert p.gas_usdt == Decimal("0.072")
        assert p.gas_bnb == Decimal("0.00012")
        assert p.bnb_usdt_rate == Decimal("600")

    notes = await _notifications(sessionmaker, 930)
    assert notes[0].kind == notif_repo.PAYOUT_PAID
    # notified amount = (net - gas) floored to cents = 14.56 (matches the on-chain send)
    assert Decimal(notes[0].payload["net_usdt"]) == Decimal("14.56")
    assert Decimal(notes[0].payload["gas_usdt"]) == Decimal("0.072")


async def test_reverted_enqueues_failed_and_dev_alert(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 911, net_usdt=Decimal("20"))

    executor = _make_executor(sessionmaker, chain, cipher, developer_id=42)
    await executor.process_next()  # -> sent
    async with sessionmaker() as session:
        sent = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert sent is not None and sent.nonce is not None
        nonce = sent.nonce
    chain.set_status(nonce, "failed")  # tx reverts on-chain
    await executor.poll_confirmations()

    user_notes = await _notifications(sessionmaker, 911)
    assert [n.kind for n in user_notes] == [notif_repo.PAYOUT_FAILED]
    dev_notes = await _notifications(sessionmaker, 42)
    assert [n.kind for n in dev_notes] == [notif_repo.DEV_ALERT]


async def test_sent_with_enough_confirmations_becomes_paid(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 901)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        sent = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert sent is not None
        assert sent.status == "sent"
        nonce = sent.nonce
        assert nonce is not None

    await executor.poll_confirmations()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "paid"
        assert refreshed.confirmed_at == FIXED_NOW
        assert refreshed.paid_at == FIXED_NOW

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 901))
        ).all()
        assert len(accruals) == 1
        assert accruals[0].state == "paid"
        assert accruals[0].payout_id == payout_id


async def test_sent_with_too_few_confirmations_stays_sent_then_finalizes(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=5, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 902)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()

    async with sessionmaker() as session:
        sent = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert sent is not None
        nonce = sent.nonce
        assert nonce is not None

    await executor.poll_confirmations()

    async with sessionmaker() as session:
        still_sent = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert still_sent is not None
        assert still_sent.status == "sent"
        assert still_sent.confirmed_at is None
        assert still_sent.paid_at is None

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 902))
        ).all()
        assert len(accruals) == 1
        assert accruals[0].state == "reserved"

    chain.set_confirmations(nonce, 15)
    await executor.poll_confirmations()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "paid"

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 902))
        ).all()
        assert accruals[0].state == "paid"


async def test_paid_payout_anchors_cooldown_via_last_success_withdrawal_at(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 903)

    executor = _make_executor(sessionmaker, chain, cipher)
    await executor.process_next()
    await executor.poll_confirmations()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "paid"

        anchor = await botui_repo.last_success_withdrawal_at(session, 903)
        assert anchor is not None
        assert anchor == refreshed.confirmed_at == FIXED_NOW


async def test_reverted_tx_becomes_failed_and_returns_accruals(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=0, default_confirmations=15, default_status="success")
    payout_id = await _seed_reserved_payout_flow(sessionmaker, cipher, 904)

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
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.failed_reason == "reverted"

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 904))
        ).all()
        assert len(accruals) == 1
        assert accruals[0].state == "available"
        assert accruals[0].payout_id is None
