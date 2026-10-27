# tests/botui/test_withdraw_blocked.py
"""Kill-switch + blocked-user enforcement in `service.prepare_withdrawal` (Task 6).

A paused ``payouts_paused`` flag or a `blocked` user status must short-circuit
before any balance/eligibility computation — checked ahead of `no_wallet` etc.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin import repository as admin_repo
from bot.botui import repository as repo
from bot.botui import service
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.payouts import Payout
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 7, 20, tzinfo=UTC)
TODAY = date(2026, 7, 20)


@pytest.fixture(autouse=True)
def _mnt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module's MNT-cross-asset scenario expects the MNT path to be reachable
    (FIX M3 otherwise gates it on settings.mnt_enabled — see test_withdraw.py for
    that gate's own dedicated tests)."""
    settings = make_settings(mantle_rpc_url="https://rpc.mantle.xyz")
    monkeypatch.setattr(service, "get_settings", lambda: settings)


async def _user_with_wallet(
    session: AsyncSession, user_id: int, *, status: str = "active"
) -> ExchangeAccount:
    session.add(User(id=user_id, role="user", status=status))
    await session.flush()
    session.add(
        Wallet(
            user_id=user_id,
            address_encrypted=b"enc",
            address_hash=f"hash{user_id}".encode(),
            address_masked="0x1234…abcd",
        )
    )
    acc = ExchangeAccount(
        user_id=user_id,
        exchange="bybit",
        uid=f"U{user_id}",
        status="active",
        accrual_since=date(2026, 7, 1),
    )
    session.add(acc)
    await session.flush()
    return acc


def _accrual(
    user_id: int, account_id: int, d: date, amount: Decimal, rank: int, fee: Decimal
) -> RebateAccrual:
    commission = amount * fee
    return RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=d,
        amount_usdt=amount,
        effective_rank=rank,
        fee_pct=fee,
        commission_usdt=commission,
        net_usdt=amount - commission,
        state="available",
        available_at=datetime(2026, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 10, tzinfo=UTC),
        raw_records_count=1,
        adapter_notes=None,
    )


async def test_prepare_withdrawal_blocked_when_paused(db_session: AsyncSession) -> None:
    session = db_session
    acc = await _user_with_wallet(session, 700)
    session.add(_accrual(700, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10")))
    await admin_repo.set_flag(session, admin_repo.PAYOUTS_PAUSED_KEY, {"paused": True}, by=700)
    await session.flush()

    result = await service.prepare_withdrawal(session, 700, NOW, TODAY)

    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "paused"


async def test_prepare_withdrawal_blocked_when_user_blocked(db_session: AsyncSession) -> None:
    session = db_session
    acc = await _user_with_wallet(session, 701, status="blocked")
    session.add(_accrual(701, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10")))
    await session.flush()

    result = await service.prepare_withdrawal(session, 701, NOW, TODAY)

    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "blocked"


async def test_prepare_withdrawal_normal_when_not_paused_and_not_blocked(
    db_session: AsyncSession,
) -> None:
    session = db_session
    acc = await _user_with_wallet(session, 702)
    session.add(_accrual(702, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10")))
    await session.flush()

    result = await service.prepare_withdrawal(session, 702, NOW, TODAY)

    assert isinstance(result, service.WithdrawPreview)
    assert result.net == Decimal("45.0")


async def test_prepare_withdrawal_mnt_not_blocked_by_active_usdt_payout(
    db_session: AsyncSession,
) -> None:
    """Per-asset independence (was: shared invariant): a pending USDT payout must NOT
    block starting an MNT withdrawal — only another USDT one. Confirms the same user,
    same tick: MNT proceeds to a preview while a second USDT is still blocked 'pending'."""
    session = db_session
    acc = await _user_with_wallet(session, 712)
    # A USDT accrual (drives rank -> payable) plus an available MNT balance to withdraw.
    session.add(_accrual(712, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10")))
    session.add(
        RebateAccrual(
            user_id=712,
            account_id=acc.id,
            accrual_date=date(2026, 7, 6),
            asset="MNT",
            amount_usdt=Decimal("60"),
            effective_rank=1,
            fee_pct=Decimal("0.10"),
            commission_usdt=Decimal("6"),
            net_usdt=Decimal("54"),
            amount_native=Decimal("100"),
            net_native=Decimal("90"),
            fx_rate=Decimal("0.6"),
            state="available",
            available_at=datetime(2026, 7, 12, tzinfo=UTC),
            expires_at=datetime(2026, 8, 10, tzinfo=UTC),
            raw_records_count=1,
            adapter_notes=None,
        )
    )
    await session.flush()
    wallet = await repo.get_active_wallet(session, 712)
    assert wallet is not None
    # An active (reserved) USDT payout — the thing that used to block everything.
    session.add(
        Payout(
            user_id=712,
            wallet_id=wallet.id,
            address_encrypted=b"x",
            address_masked="0x..abcd",
            covered_from=date(2026, 7, 1),
            covered_to=date(2026, 7, 31),
            gross_usdt=Decimal("10"),
            commission_usdt=Decimal("1"),
            net_usdt=Decimal("9"),
            asset="USDT",
            status="reserved",
            idempotency_key="idem-712-usdt-reserved",
        )
    )
    await session.flush()

    # MNT is a DIFFERENT pool -> the active USDT payout does not block it.
    mnt = await service.prepare_withdrawal(session, 712, NOW, TODAY, asset="MNT")
    assert isinstance(mnt, service.WithdrawPreview)
    assert mnt.asset == "MNT"

    # A second USDT withdrawal IS still blocked by the active USDT payout (same asset).
    usdt = await service.prepare_withdrawal(session, 712, NOW, TODAY, asset="USDT")
    assert isinstance(usdt, service.WithdrawBlocked)
    assert usdt.reason == "pending"


# --- confirm-time TOCTOU: pause/block flipped on DURING the 5-min token window --------


async def test_confirm_withdrawal_blocked_when_paused_after_token_issued(
    db_session: AsyncSession,
) -> None:
    """The user passed prepare and holds a live token; an admin pauses payouts before
    they confirm. confirm_withdrawal must refuse — no reserved payout is created."""
    session = db_session
    acc = await _user_with_wallet(session, 710)
    session.add(_accrual(710, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10")))
    await session.flush()
    preview = await service.prepare_withdrawal(session, 710, NOW, TODAY)
    assert isinstance(preview, service.WithdrawPreview)
    token = await service.issue_withdraw_token(session, 710, preview, NOW)

    # admin pauses AFTER the token is issued
    await admin_repo.set_flag(session, admin_repo.PAYOUTS_PAUSED_KEY, {"paused": True}, by=710)
    await session.flush()

    result = await service.confirm_withdrawal(session, 710, token, NOW)
    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "paused"
    # nothing reserved, accruals untouched
    assert (await session.scalar(select(func.count()).select_from(Payout))) == 0
    accruals = await repo.available_accruals(session, 710)
    assert len(accruals) == 1 and accruals[0].state == "available"


async def test_confirm_withdrawal_blocked_when_user_blocked_after_token_issued(
    db_session: AsyncSession,
) -> None:
    """User blocked by an admin during the token window -> confirm refuses cleanly."""
    session = db_session
    acc = await _user_with_wallet(session, 711)
    session.add(_accrual(711, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10")))
    await session.flush()
    preview = await service.prepare_withdrawal(session, 711, NOW, TODAY)
    assert isinstance(preview, service.WithdrawPreview)
    token = await service.issue_withdraw_token(session, 711, preview, NOW)

    await admin_repo.set_user_status(session, 711, "blocked")
    await session.flush()

    result = await service.confirm_withdrawal(session, 711, token, NOW)
    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "blocked"
    assert (await session.scalar(select(func.count()).select_from(Payout))) == 0
