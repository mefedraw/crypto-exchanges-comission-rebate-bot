# tests/botui/test_withdraw_per_asset_cooldown.py
"""E2E coverage for INDEPENDENT USDT/MNT withdrawal timelines.

Each pool has its own cooldown clock and its own one-active-payout limit: withdrawing
(or having a pending payout in) one asset must never delay or block the other. These
tests exercise the concrete situations that can trip that up — cross-asset leakage,
the migration-backfilled legacy `asset`, the pending_admin MNT hold, and the cooldown
boundary — end to end through `service.balance_summary` / `prepare_withdrawal` and the
`repository` gates they rest on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo
from bot.botui import service
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.payouts import Payout
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Fixed clock: accruals live in August, so rank is driven by August month-gross and the
# available window (available_at < NOW < expires_at) holds.
NOW = datetime(2026, 8, 15, tzinfo=UTC)
TODAY = date(2026, 8, 15)
COOLDOWN = timedelta(days=30)  # rank 1 (COOLDOWN_DAYS[1]); gross kept in [10, 100]


@pytest.fixture(autouse=True)
def _mnt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """MNT withdraw is gated on settings.mnt_enabled (MANTLE_RPC_URL); enable it so the
    MNT path is reachable in prepare_withdrawal."""
    settings = make_settings(mantle_rpc_url="https://rpc.mantle.xyz")
    monkeypatch.setattr(service, "get_settings", lambda: settings)


async def _seed_user(session: AsyncSession, uid: int) -> ExchangeAccount:
    session.add(User(id=uid, role="user", status="active"))
    await session.flush()
    session.add(
        Wallet(
            user_id=uid,
            address_encrypted=b"enc",
            address_hash=f"hash{uid}".encode(),
            address_masked="0x1234…abcd",
        )
    )
    acc = ExchangeAccount(
        user_id=uid,
        exchange="bybit",
        uid=f"U{uid}",
        status="active",
        accrual_since=date(2026, 8, 1),
    )
    session.add(acc)
    await session.flush()
    return acc


def _accrual(
    uid: int,
    account_id: int,
    *,
    asset: str = "USDT",
    amount: Decimal = Decimal("30"),
    net: Decimal = Decimal("27"),
    net_native: Decimal | None = None,
) -> RebateAccrual:
    """One available accrual on 2026-08-01. `amount` (gross USDT) drives rank; `net`/
    `net_native` are the frozen payable figures balance_summary sums verbatim."""
    return RebateAccrual(
        user_id=uid,
        account_id=account_id,
        accrual_date=date(2026, 8, 1),
        asset=asset,
        amount_usdt=amount,
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        commission_usdt=amount - net,
        net_usdt=net,
        amount_native=amount if net_native is None else amount,
        net_native=net if net_native is None else net_native,
        fx_rate=Decimal("1") if net_native is None else (net / net_native),
        state="available",
        available_at=datetime(2026, 8, 2, tzinfo=UTC),
        expires_at=datetime(2026, 9, 1, tzinfo=UTC),
        raw_records_count=1,
        adapter_notes=None,
    )


async def _add_payout(
    session: AsyncSession,
    uid: int,
    *,
    status: str,
    asset: str | None = "USDT",
    confirmed_at: datetime | None = None,
    sent_at: datetime | None = None,
    net_usdt: Decimal = Decimal("20"),
    key: str,
) -> Payout:
    """Create a payout. `asset=None` omits the column entirely so the DB server_default
    ('USDT', backfilled onto legacy rows by migration 536cf94b2929) applies — the legacy
    case we must keep counting as USDT."""
    wallet_row = await repo.get_active_wallet(session, uid)
    assert wallet_row is not None
    kwargs: dict[str, object] = {
        "user_id": uid,
        "wallet_id": wallet_row.id,
        "address_encrypted": b"x",
        "address_masked": "0x..abcd",
        "covered_from": date(2026, 8, 1),
        "covered_to": date(2026, 8, 1),
        "gross_usdt": net_usdt,
        "commission_usdt": Decimal("0"),
        "net_usdt": net_usdt,
        "status": status,
        "sent_at": sent_at,
        "confirmed_at": confirmed_at,
        "idempotency_key": key,
    }
    if asset is not None:
        kwargs["asset"] = asset
        kwargs["net_native"] = net_usdt
    payout = Payout(**kwargs)  # type: ignore[arg-type]
    session.add(payout)
    await session.flush()
    return payout


# --- balance_summary: cross-asset independence --------------------------------------


async def test_usdt_withdrawal_does_not_delay_mnt(db_session: AsyncSession) -> None:
    """A recent USDT payout puts USDT on cooldown but leaves MNT immediately withdrawable
    (MNT was never withdrawn)."""
    session = db_session
    acc = await _seed_user(session, 900)
    session.add(_accrual(900, acc.id, asset="USDT"))
    session.add(_accrual(900, acc.id, asset="MNT", net=Decimal("27"), net_native=Decimal("45")))
    await _add_payout(
        session,
        900,
        status="confirmed",
        asset="USDT",
        confirmed_at=NOW - timedelta(days=5),
        key="p900",
    )

    s = await service.balance_summary(session, 900, NOW, TODAY)
    assert s.withdrawable is False  # USDT on cooldown (confirmed 5d ago, 30d cooldown)
    assert s.mnt_withdrawable is True  # MNT untouched -> free
    assert s.next_withdrawal_at == (NOW - timedelta(days=5)) + COOLDOWN
    assert s.mnt_next_withdrawal_at is None  # no MNT payout ever


async def test_mnt_withdrawal_does_not_delay_usdt(db_session: AsyncSession) -> None:
    """Mirror: a recent MNT payout puts only MNT on cooldown; USDT stays withdrawable."""
    session = db_session
    acc = await _seed_user(session, 901)
    session.add(_accrual(901, acc.id, asset="USDT"))
    session.add(_accrual(901, acc.id, asset="MNT", net=Decimal("27"), net_native=Decimal("45")))
    await _add_payout(
        session,
        901,
        status="confirmed",
        asset="MNT",
        confirmed_at=NOW - timedelta(days=5),
        key="p901",
    )

    s = await service.balance_summary(session, 901, NOW, TODAY)
    assert s.withdrawable is True  # USDT untouched -> free
    assert s.mnt_withdrawable is False  # MNT on cooldown
    assert s.next_withdrawal_at is None
    assert s.mnt_next_withdrawal_at == (NOW - timedelta(days=5)) + COOLDOWN


async def test_per_asset_next_withdrawal_timestamps_are_independent(
    db_session: AsyncSession,
) -> None:
    """Two payouts confirmed at different times drive two independent cooldown ends."""
    session = db_session
    acc = await _seed_user(session, 902)
    session.add(_accrual(902, acc.id, asset="USDT"))
    session.add(_accrual(902, acc.id, asset="MNT", net=Decimal("27"), net_native=Decimal("45")))
    usdt_at = NOW - timedelta(days=3)
    mnt_at = NOW - timedelta(days=10)
    await _add_payout(session, 902, status="confirmed", asset="USDT", confirmed_at=usdt_at, key="u")
    await _add_payout(session, 902, status="confirmed", asset="MNT", confirmed_at=mnt_at, key="m")

    s = await service.balance_summary(session, 902, NOW, TODAY)
    assert s.next_withdrawal_at == usdt_at + COOLDOWN
    assert s.mnt_next_withdrawal_at == mnt_at + COOLDOWN


async def test_last_payout_line_is_global_max_across_assets(db_session: AsyncSession) -> None:
    """The informational "last payout" is the most-recent successful payout of ANY asset."""
    session = db_session
    acc = await _seed_user(session, 903)
    session.add(_accrual(903, acc.id, asset="USDT"))
    older = NOW - timedelta(days=20)
    newer = NOW - timedelta(days=2)
    await _add_payout(session, 903, status="confirmed", asset="USDT", confirmed_at=older, key="o")
    await _add_payout(session, 903, status="confirmed", asset="MNT", confirmed_at=newer, key="n")

    s = await service.balance_summary(session, 903, NOW, TODAY)
    assert s.last_withdrawal_at == newer  # max(older, newer)


async def test_legacy_payout_counts_as_usdt_cooldown_not_mnt(db_session: AsyncSession) -> None:
    """A legacy payout predating the `asset` column (backfilled to 'USDT') must gate the
    USDT cooldown, never MNT. Guards the migration-backfill assumption end to end."""
    session = db_session
    acc = await _seed_user(session, 904)
    session.add(_accrual(904, acc.id, asset="USDT"))
    session.add(_accrual(904, acc.id, asset="MNT", net=Decimal("27"), net_native=Decimal("45")))
    # asset=None -> column omitted -> DB server_default 'USDT'.
    await _add_payout(
        session,
        904,
        status="confirmed",
        asset=None,
        confirmed_at=NOW - timedelta(days=5),
        key="legacy904",
    )

    s = await service.balance_summary(session, 904, NOW, TODAY)
    assert s.withdrawable is False  # legacy payout treated as USDT -> USDT on cooldown
    assert s.mnt_withdrawable is True  # ...and MNT untouched


async def test_cooldown_boundary_now_equals_next_is_allowed(db_session: AsyncSession) -> None:
    """Boundary: at exactly last_success + cooldown the withdrawal is allowed (>=)."""
    session = db_session
    acc = await _seed_user(session, 905)
    session.add(_accrual(905, acc.id, asset="USDT"))
    await _add_payout(
        session,
        905,
        status="confirmed",
        asset="USDT",
        confirmed_at=NOW - COOLDOWN,
        key="b",
    )

    s = await service.balance_summary(session, 905, NOW, TODAY)
    assert s.next_withdrawal_at == NOW  # cooldown ends exactly now
    assert s.withdrawable is True  # now >= next_at -> allowed


# --- prepare_withdrawal: per-asset cooldown + pending gates --------------------------


async def test_prepare_usdt_cooldown_independent_of_mnt(db_session: AsyncSession) -> None:
    """A recent MNT payout blocks MNT on cooldown but lets USDT proceed to a preview."""
    session = db_session
    acc = await _seed_user(session, 910)
    session.add(_accrual(910, acc.id, asset="USDT"))
    session.add(_accrual(910, acc.id, asset="MNT", net=Decimal("27"), net_native=Decimal("45")))
    await _add_payout(
        session,
        910,
        status="confirmed",
        asset="MNT",
        confirmed_at=NOW - timedelta(days=5),
        key="p910",
    )

    usdt = await service.prepare_withdrawal(session, 910, NOW, TODAY, asset="USDT")
    assert isinstance(usdt, service.WithdrawPreview)

    mnt = await service.prepare_withdrawal(session, 910, NOW, TODAY, asset="MNT")
    assert isinstance(mnt, service.WithdrawBlocked)
    assert mnt.reason == "cooldown"


async def test_prepare_mnt_cooldown_independent_of_usdt(db_session: AsyncSession) -> None:
    """Mirror: a recent USDT payout blocks USDT on cooldown but lets MNT proceed."""
    session = db_session
    acc = await _seed_user(session, 911)
    session.add(_accrual(911, acc.id, asset="USDT"))
    session.add(_accrual(911, acc.id, asset="MNT", net=Decimal("27"), net_native=Decimal("45")))
    await _add_payout(
        session,
        911,
        status="confirmed",
        asset="USDT",
        confirmed_at=NOW - timedelta(days=5),
        key="p911",
    )

    mnt = await service.prepare_withdrawal(session, 911, NOW, TODAY, asset="MNT")
    assert isinstance(mnt, service.WithdrawPreview)

    usdt = await service.prepare_withdrawal(session, 911, NOW, TODAY, asset="USDT")
    assert isinstance(usdt, service.WithdrawBlocked)
    assert usdt.reason == "cooldown"


async def test_pending_admin_mnt_blocks_mnt_but_not_usdt(db_session: AsyncSession) -> None:
    """The scenario that motivated per-asset: an MNT payout parked in pending_admin
    (awaiting admin action, possibly for days) must block only another MNT withdrawal,
    never USDT."""
    session = db_session
    acc = await _seed_user(session, 912)
    session.add(_accrual(912, acc.id, asset="USDT"))
    session.add(_accrual(912, acc.id, asset="MNT", net=Decimal("27"), net_native=Decimal("45")))
    await _add_payout(session, 912, status="pending_admin", asset="MNT", key="p912")

    usdt = await service.prepare_withdrawal(session, 912, NOW, TODAY, asset="USDT")
    assert isinstance(usdt, service.WithdrawPreview)  # USDT free

    mnt = await service.prepare_withdrawal(session, 912, NOW, TODAY, asset="MNT")
    assert isinstance(mnt, service.WithdrawBlocked)
    assert mnt.reason == "pending"


# --- repository gates: the asset filter in isolation --------------------------------


async def test_repo_last_success_asset_filter_excludes_other_asset(
    db_session: AsyncSession,
) -> None:
    session = db_session
    await _seed_user(session, 920)
    at = NOW - timedelta(days=1)
    await _add_payout(session, 920, status="confirmed", asset="MNT", confirmed_at=at, key="mnt920")

    assert await repo.last_success_withdrawal_at(session, 920, asset="USDT") is None
    assert await repo.last_success_withdrawal_at(session, 920, asset="MNT") == at
    assert await repo.last_success_withdrawal_at(session, 920) == at  # global spans both


async def test_repo_has_active_payout_asset_filter(db_session: AsyncSession) -> None:
    session = db_session
    await _seed_user(session, 921)
    await _add_payout(session, 921, status="reserved", asset="USDT", key="res921")

    assert await repo.has_active_payout(session, 921, asset="USDT") is True
    assert await repo.has_active_payout(session, 921, asset="MNT") is False
    assert await repo.has_active_payout(session, 921) is True  # global -> any active
