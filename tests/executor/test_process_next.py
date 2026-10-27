# tests/executor/test_process_next.py
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
from bot.db.models.ops import Notification
from bot.db.models.payouts import Payout
from bot.executor.chain import MockChainClient, native_to_wei, usdt_to_wei
from bot.executor.executor import PayoutExecutor
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
        # cheap cleanup: clear the tables this module touches (respecting FKs)
        await session.execute(delete(RebateAccrual))
        await session.execute(delete(Notification))
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
    approved_at: datetime | None = None,
    approved_by: int | None = None,
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
        approved_at=approved_at,
        approved_by=approved_by,
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


async def test_reserved_within_limits_is_sent(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=5)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 801)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            801,
            wallet.id,
            "reserved",
            net_usdt=Decimal("45"),
            address_encrypted=addr_enc,
        )
        payout_id = payout.id
        session.add(_accrual(801, acc.id, date(2026, 7, 5), Decimal("45"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.nonce == 5
        assert refreshed.txhash is not None
        assert refreshed.sent_at == FIXED_NOW

    assert len(chain.sent_calls) == 1
    call = chain.sent_calls[-1]
    assert call["to"] == RECIPIENT
    assert call["amount_wei"] == usdt_to_wei(Decimal("45"))
    assert call["nonce"] == 5


async def test_reserved_uses_max_of_onchain_and_last_db_nonce(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=1)  # on-chain nonce is behind the DB
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 802)
        # a previously-sent payout already used nonce=9
        await _seed_payout(
            session,
            802,
            wallet.id,
            "paid",
            net_usdt=Decimal("1"),
            address_encrypted=b"irrelevant",
            nonce=9,
        )
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            802,
            wallet.id,
            "reserved",
            net_usdt=Decimal("20"),
            address_encrypted=addr_enc,
        )
        payout_id = payout.id
        session.add(_accrual(802, acc.id, date(2026, 7, 5), Decimal("20"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.nonce == 10  # max(1, 9+1)


async def test_over_auto_limit_goes_pending_admin_without_sending(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """FINDING 1(b): a NOT-yet-approved over-limit payout still goes to pending_admin —
    unchanged behavior. ``approved_at`` is None here (never released by an admin)."""
    chain = MockChainClient(nonce=1)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 803)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            803,
            wallet.id,
            "reserved",
            net_usdt=Decimal("250"),
            address_encrypted=addr_enc,
        )
        payout_id = payout.id
        session.add(_accrual(803, acc.id, date(2026, 7, 5), Decimal("250"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "pending_admin"

    assert chain.sent_calls == []


async def test_over_auto_limit_but_admin_approved_is_sent_not_bounced(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """FINDING 1(a): a LARGE (net_usdt > auto_limit) payout that an admin already
    released (approved_at/approved_by set, status flipped back to 'reserved' by
    admin_repo.release_payout) must be SENT by process_next, not bounced back to
    pending_admin. Before the fix, `process_next` re-checked ``net > auto_limit`` on
    every tick with no memory of the admin's approval, so it ping-ponged forever."""
    chain = MockChainClient(nonce=1)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 807)
        session.add(User(id=9999, role="admin", status="active"))
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            807,
            wallet.id,
            "reserved",
            net_usdt=Decimal("250"),
            address_encrypted=addr_enc,
            approved_at=FIXED_NOW,
            approved_by=9999,
        )
        payout_id = payout.id
        session.add(_accrual(807, acc.id, date(2026, 7, 5), Decimal("250"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.txhash is not None

    assert len(chain.sent_calls) == 1


async def test_over_auto_limit_mnt_admin_approved_sends_via_mantle(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """FINDING 1(a) for MNT specifically: an over-limit MNT payout that has been
    admin-released (approved_at set) actually sends via the Mantle native client,
    rather than bouncing back to pending_admin forever (MNT is always admin-gated
    on the way in, so this is the only path an MNT payout above auto_limit can ever
    reach 'sent')."""
    bsc_chain = MockChainClient(nonce=1)
    mantle_chain = MockChainClient(nonce=5)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 808)
        session.add(User(id=9999, role="admin", status="active"))
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            808,
            wallet.id,
            "reserved",
            net_usdt=Decimal("250"),  # over auto_limit
            address_encrypted=addr_enc,
            asset="MNT",
            net_native=Decimal("500"),
            approved_at=FIXED_NOW,
            approved_by=9999,
        )
        payout_id = payout.id
        session.add(_accrual(808, acc.id, date(2026, 7, 5), Decimal("250"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        bsc_chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        mantle_chain=mantle_chain,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.nonce == 5

    assert len(mantle_chain.native_sent_calls) == 1
    assert mantle_chain.native_sent_calls[-1]["amount_wei"] == native_to_wei(Decimal("500"))
    assert bsc_chain.sent_calls == []
    assert bsc_chain.native_sent_calls == []


async def test_below_min_payout_fails_and_returns_accruals(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=1)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 804)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            804,
            wallet.id,
            "reserved",
            net_usdt=Decimal("5"),
            address_encrypted=addr_enc,
        )
        payout_id = payout.id
        session.add(_accrual(804, acc.id, date(2026, 7, 5), Decimal("5"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.failed_reason == "below_min"

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 804))
        ).all()
        assert len(accruals) == 1
        assert accruals[0].state == "available"
        assert accruals[0].payout_id is None

    assert chain.sent_calls == []


async def test_single_flight_skips_when_a_payout_is_already_in_flight(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=1)
    async with sessionmaker() as session:
        _acc, wallet = await _seed_user_wallet(session, 805)
        # an existing 'sent' payout occupies the single flight slot
        await _seed_payout(
            session,
            805,
            wallet.id,
            "sent",
            net_usdt=Decimal("1"),
            address_encrypted=b"irrelevant",
            nonce=1,
        )
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        reserved = await _seed_payout(
            session,
            805,
            wallet.id,
            "reserved",
            net_usdt=Decimal("20"),
            address_encrypted=addr_enc,
        )
        reserved_id = reserved.id
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == reserved_id))
        assert refreshed is not None
        assert refreshed.status == "reserved"

    assert chain.sent_calls == []


async def test_broadcast_chain_error_leaves_payout_submitting_no_resend(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    chain = MockChainClient(nonce=3, fail_send_no_land=True)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 806)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            806,
            wallet.id,
            "reserved",
            net_usdt=Decimal("20"),
            address_encrypted=addr_enc,
        )
        payout_id = payout.id
        session.add(_accrual(806, acc.id, date(2026, 7, 5), Decimal("20"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "submitting"
        # #3: the deterministic hash is persisted BEFORE broadcast, so it is set even
        # though this broadcast failed with nothing landing. It's harmless — reconcile
        # gates promotion on the on-chain nonce-used check, not on this hash being set.
        assert refreshed.txhash == MockChainClient._hash_for(3)
        assert refreshed.nonce == 3

    assert len(chain.sent_calls) == 1

    # running process_next again must NOT attempt a second send: single-flight
    # sees the 'submitting' payout and returns immediately.
    await executor.process_next()
    assert len(chain.sent_calls) == 1


# ---------------------------------------------------------------------------
# Task 9: MNT native transfer on Mantle — asset-aware routing
# ---------------------------------------------------------------------------


async def test_reserved_mnt_payout_sent_native_on_mantle_not_bsc(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """A `reserved` MNT payout is sent as a NATIVE transfer on the Mantle mock client —
    the full `net_native` amount, no gas deduction — and never touches the BSC client."""
    bsc_chain = MockChainClient(nonce=1)
    mantle_chain = MockChainClient(nonce=5)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 820)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            820,
            wallet.id,
            "reserved",
            net_usdt=Decimal("60"),  # USDT-equivalent, well under auto_limit
            address_encrypted=addr_enc,
            asset="MNT",
            net_native=Decimal("100"),
        )
        payout_id = payout.id
        session.add(_accrual(820, acc.id, date(2026, 7, 5), Decimal("60"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        bsc_chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        mantle_chain=mantle_chain,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.nonce == 5  # Mantle's own nonce space, not BSC's
        assert refreshed.txhash is not None
        assert refreshed.gas_usdt == Decimal("0")  # gas borne by the owner, never deducted

    # exactly one NATIVE send, on the Mantle client, for the full net_native amount
    assert len(mantle_chain.native_sent_calls) == 1
    call = mantle_chain.native_sent_calls[-1]
    assert call["to"] == RECIPIENT
    assert call["amount_wei"] == native_to_wei(Decimal("100"))
    assert call["nonce"] == 5

    # the BSC (token) client was never touched
    assert mantle_chain.sent_calls == []
    assert bsc_chain.sent_calls == []
    assert bsc_chain.native_sent_calls == []


async def test_reserved_usdt_payout_still_uses_bsc_with_gas_deduction_when_mantle_present(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """Adding a Mantle client for MNT must not change the USDT/BSC path at all: gas is
    still quoted and deducted, the send still goes out as a BSC token transfer."""
    from bot.executor.chain import GasQuote

    gas = GasQuote(
        gas_bnb=Decimal("0.00012"), gas_usdt=Decimal("0.072"), bnb_usdt_rate=Decimal("600")
    )
    bsc_chain = MockChainClient(nonce=1, gas_quote=gas)
    mantle_chain = MockChainClient(nonce=9)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 821)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            821,
            wallet.id,
            "reserved",
            net_usdt=Decimal("14.64"),
            address_encrypted=addr_enc,
        )
        payout_id = payout.id
        session.add(
            _accrual(821, acc.id, date(2026, 7, 5), Decimal("14.64"), "reserved", payout_id)
        )
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        bsc_chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        mantle_chain=mantle_chain,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.nonce == 1
        assert refreshed.gas_usdt == Decimal("0.072")

    assert len(bsc_chain.sent_calls) == 1
    assert bsc_chain.sent_calls[-1]["amount_wei"] == usdt_to_wei(
        Decimal("14.64") - Decimal("0.072")
    )
    # the Mantle client was never touched by a USDT payout
    assert mantle_chain.sent_calls == []
    assert mantle_chain.native_sent_calls == []


async def _dev_alert_count(session: AsyncSession, developer_id: int) -> int:
    rows = await session.scalars(
        select(Notification).where(
            Notification.recipient_chat_id == developer_id, Notification.kind == "dev_alert"
        )
    )
    return len(list(rows.all()))


async def test_usdt_insufficient_token_balance_holds_reserved_and_alerts(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """Wallet has too little USDT for the transfer: the payout is HELD 'reserved' (never
    broadcast, so it can't revert on-chain like prod #10 did), and the developer is
    alerted once. The money stays reserved for the user; it auto-retries once funded."""
    chain = MockChainClient(nonce=5, token_balance_wei=usdt_to_wei(Decimal("10")))
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 830)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session, 830, wallet.id, "reserved", net_usdt=Decimal("45"), address_encrypted=addr_enc
        )
        payout_id = payout.id
        session.add(_accrual(830, acc.id, date(2026, 7, 5), Decimal("45"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        developer_id=555001,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "reserved"  # held, NOT submitting/failed
        assert refreshed.nonce is None
        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 830))
        ).all()
        assert accruals[0].state == "reserved"  # money stays reserved for the user
        assert await _dev_alert_count(session, 555001) == 1

    assert chain.sent_calls == []  # nothing broadcast


async def test_usdt_enough_token_but_no_gas_holds_reserved(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """Enough USDT but no BNB for gas -> still held 'reserved' (a send would fail),
    reported as a BNB (gas) shortfall."""
    chain = MockChainClient(nonce=5, native_balance_wei=0, gas_price_wei=1)  # token unlimited
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 831)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session, 831, wallet.id, "reserved", net_usdt=Decimal("45"), address_encrypted=addr_enc
        )
        payout_id = payout.id
        session.add(_accrual(831, acc.id, date(2026, 7, 5), Decimal("45"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "reserved"
    assert chain.sent_calls == []


async def test_mnt_insufficient_native_holds_reserved(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """The prod #11 scenario: wallet holds less MNT than the payout needs -> held
    'reserved' (never reaches the stuck 'submitting' state) and auto-retries once funded."""
    bsc_chain = MockChainClient(nonce=1)
    mantle_chain = MockChainClient(nonce=5, native_balance_wei=native_to_wei(Decimal("100")))
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 832)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            832,
            wallet.id,
            "reserved",
            net_usdt=Decimal("60"),
            address_encrypted=addr_enc,
            asset="MNT",
            net_native=Decimal("500"),  # need 500 MNT, wallet has 100
        )
        payout_id = payout.id
        session.add(_accrual(832, acc.id, date(2026, 7, 5), Decimal("60"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        bsc_chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        mantle_chain=mantle_chain,
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "reserved"
        assert refreshed.nonce is None
    assert mantle_chain.native_sent_calls == []


async def test_insufficient_then_funded_sends_automatically(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """The whole point: after the owner tops up the wallet, the HELD payout goes out on
    the next tick with NO manual intervention. Simulated by raising the mock's balance
    between two process_next calls."""
    chain = MockChainClient(nonce=5, token_balance_wei=usdt_to_wei(Decimal("10")))
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 833)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session, 833, wallet.id, "reserved", net_usdt=Decimal("45"), address_encrypted=addr_enc
        )
        payout_id = payout.id
        session.add(_accrual(833, acc.id, date(2026, 7, 5), Decimal("45"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        developer_id=555002,
    )
    await executor.process_next()  # held (insufficient)
    assert chain.sent_calls == []

    # owner funds the wallet
    chain._token_balance_wei = usdt_to_wei(Decimal("1000"))
    await executor.process_next()  # now sends, no manual step

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.nonce == 5
    assert len(chain.sent_calls) == 1


async def test_insufficient_funds_alert_is_deduped_across_ticks(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """A held payout re-checked on every 30s tick must alert the developer ONCE, not
    on every tick."""
    chain = MockChainClient(nonce=5, token_balance_wei=usdt_to_wei(Decimal("10")))
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 834)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session, 834, wallet.id, "reserved", net_usdt=Decimal("45"), address_encrypted=addr_enc
        )
        payout_id = payout.id
        session.add(_accrual(834, acc.id, date(2026, 7, 5), Decimal("45"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        developer_id=555003,
    )
    await executor.process_next()
    await executor.process_next()
    await executor.process_next()

    async with sessionmaker() as session:
        assert await _dev_alert_count(session, 555003) == 1  # deduped, not 3


async def test_reserved_mnt_payout_without_mantle_client_fails_disabled(
    sessionmaker: async_sessionmaker[AsyncSession], cipher: SecretCipher
) -> None:
    """A reserved MNT payout claimed while the Mantle client is unavailable (feature
    disabled / not injected) fails closed rather than silently doing nothing — the
    money is returned to the user's available pool for later, not lost."""
    bsc_chain = MockChainClient(nonce=1)
    async with sessionmaker() as session:
        acc, wallet = await _seed_user_wallet(session, 822)
        addr_enc = cipher.encrypt(RECIPIENT).encode()
        payout = await _seed_payout(
            session,
            822,
            wallet.id,
            "reserved",
            net_usdt=Decimal("60"),
            address_encrypted=addr_enc,
            asset="MNT",
            net_native=Decimal("100"),
        )
        payout_id = payout.id
        session.add(_accrual(822, acc.id, date(2026, 7, 5), Decimal("60"), "reserved", payout_id))
        await session.commit()

    executor = PayoutExecutor(
        sessionmaker,
        bsc_chain,
        cipher,
        now=_fixed_now,
        wallet_address=MockChainClient.WALLET,
        auto_limit=Decimal("200"),
        min_payout=Decimal("10"),
        confirmations=15,
        # mantle_chain omitted -> None -> MNT disabled
    )
    await executor.process_next()

    async with sessionmaker() as session:
        refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.failed_reason == "mnt_disabled"

        accruals = (
            await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 822))
        ).all()
        assert len(accruals) == 1
        assert accruals[0].state == "available"
        assert accruals[0].payout_id is None

    assert bsc_chain.sent_calls == []
    assert bsc_chain.native_sent_calls == []
