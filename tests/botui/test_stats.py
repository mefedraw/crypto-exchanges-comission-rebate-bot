# tests/botui/test_stats.py
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo
from bot.botui import service, stats
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RankHistory, RebateAccrual
from bot.db.models.payouts import Payout

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _user_with_account(
    session: AsyncSession, user_id: int, exchange: str = "bybit", uid: str = "U"
) -> ExchangeAccount:
    session.add(User(id=user_id, role="user", status="active"))
    await session.flush()
    acc = ExchangeAccount(
        user_id=user_id,
        exchange=exchange,
        uid=f"{uid}{user_id}",
        status="active",
        accrual_since=date(2026, 7, 1),
    )
    session.add(acc)
    await session.flush()
    return acc


def _accrual(
    user_id: int,
    account_id: int,
    d: date,
    amount: Decimal,
    rank: int,
    fee: Decimal,
    state: str,
    expires_at: datetime = datetime(2026, 8, 10, tzinfo=UTC),
    *,
    asset: str = "USDT",
    amount_native: Decimal | None = None,
    net_native: Decimal | None = None,
    fx_rate: Decimal = Decimal("1"),
) -> RebateAccrual:
    commission = amount * fee
    net = amount - commission
    return RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=d,
        asset=asset,
        amount_usdt=amount,
        effective_rank=rank,
        fee_pct=fee,
        commission_usdt=commission,
        net_usdt=net,
        amount_native=amount_native if amount_native is not None else amount,
        net_native=net_native if net_native is not None else net,
        fx_rate=fx_rate,
        state=state,
        available_at=datetime(2026, 7, 12, tzinfo=UTC),
        expires_at=expires_at,
        raw_records_count=1,
        adapter_notes=None,
    )


async def _get_or_create_wallet(session: AsyncSession, user_id: int) -> Wallet:
    existing = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if existing is not None:
        return existing
    wallet = Wallet(
        user_id=user_id, address_encrypted=b"x", address_hash=b"h", address_masked="0x..abcd"
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def _payout(
    session: AsyncSession,
    user_id: int,
    status: str,
    *,
    sent_at: datetime | None = None,
    confirmed_at: datetime | None = None,
    asset: str = "USDT",
) -> Payout:
    wallet = await _get_or_create_wallet(session, user_id)
    payout = Payout(
        user_id=user_id,
        wallet_id=wallet.id,
        address_encrypted=b"x",
        address_masked="0x..abcd",
        covered_from=date(2026, 7, 1),
        covered_to=date(2026, 7, 31),
        gross_usdt=Decimal("10"),
        commission_usdt=Decimal("1"),
        net_usdt=Decimal("9"),
        asset=asset,
        net_native=Decimal("9"),
        status=status,
        sent_at=sent_at,
        confirmed_at=confirmed_at,
        idempotency_key=f"idem-{user_id}-{asset}-{status}-{sent_at}-{confirmed_at}",
    )
    session.add(payout)
    await session.flush()
    return payout


# --- repository -------------------------------------------------------------


async def test_available_accruals_only_state_available_fifo(db_session: AsyncSession):
    acc = await _user_with_account(db_session, 500)
    session = db_session
    fee = Decimal("0.10")
    session.add(_accrual(500, acc.id, date(2026, 7, 12), Decimal("20"), 1, fee, "available"))
    session.add(_accrual(500, acc.id, date(2026, 7, 11), Decimal("15"), 1, fee, "available"))
    session.add(_accrual(500, acc.id, date(2026, 7, 10), Decimal("99"), 1, fee, "expired"))
    session.add(_accrual(500, acc.id, date(2026, 7, 9), Decimal("99"), 1, fee, "paid"))
    await session.flush()

    rows = await repo.available_accruals(session, 500)
    assert [r.accrual_date for r in rows] == [date(2026, 7, 11), date(2026, 7, 12)]
    assert {r.amount_usdt for r in rows} == {Decimal("15"), Decimal("20")}


async def test_last_success_withdrawal_at_filters_status(db_session: AsyncSession):
    session = db_session
    session.add(User(id=501, role="user", status="active"))
    await session.flush()
    await _payout(session, 501, "sent", sent_at=datetime(2026, 7, 1, tzinfo=UTC))
    await _payout(
        session,
        501,
        "confirmed",
        sent_at=datetime(2026, 7, 5, tzinfo=UTC),
        confirmed_at=datetime(2026, 7, 6, tzinfo=UTC),
    )
    await _payout(session, 501, "pending_admin", sent_at=datetime(2026, 7, 20, tzinfo=UTC))

    last = await repo.last_success_withdrawal_at(session, 501)
    assert last == datetime(2026, 7, 6, tzinfo=UTC)


async def test_last_success_withdrawal_at_none_when_no_success(db_session: AsyncSession):
    session = db_session
    session.add(User(id=502, role="user", status="active"))
    await session.flush()
    await _payout(session, 502, "failed", sent_at=datetime(2026, 7, 1, tzinfo=UTC))
    assert await repo.last_success_withdrawal_at(session, 502) is None


async def test_has_active_reserved_payout(db_session: AsyncSession):
    session = db_session
    session.add(User(id=503, role="user", status="active"))
    await session.flush()
    assert await repo.has_active_reserved_payout(session, 503) is False
    await _payout(session, 503, "reserved")
    assert await repo.has_active_reserved_payout(session, 503) is True


# --- service -----------------------------------------------------------------


async def test_effective_rank_now_honors_carry_over(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_account(session, 510)
    # This month's own gross is tiny (rank 0), but June closed at rank 3.
    session.add(_accrual(510, acc.id, date(2026, 7, 5), Decimal("5"), 0, Decimal("0"), "available"))
    session.add(
        RankHistory(
            user_id=510,
            period_month=date(2026, 6, 1),
            achieved_rank=3,
            gross_month_usdt=Decimal("5000"),
        )
    )
    await session.flush()

    rank = await service.effective_rank_now(session, 510, date(2026, 7, 15))
    assert rank == 3


async def test_effective_rank_now_uses_current_month_when_higher(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_account(session, 511)
    # gross 500 -> rank_for_gross(500) == 2 (R1_MAX=100 < 500 <= R2_MAX=1000); no carry-over.
    session.add(
        _accrual(511, acc.id, date(2026, 7, 5), Decimal("500"), 2, Decimal("0.07"), "available")
    )
    await session.flush()

    rank = await service.effective_rank_now(session, 511, date(2026, 7, 15))
    assert rank == 2


async def test_balance_summary_sums_only_available(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_account(session, 520)
    fee = Decimal("0.10")
    session.add(_accrual(520, acc.id, date(2026, 7, 5), Decimal("50"), 1, fee, "available"))
    session.add(_accrual(520, acc.id, date(2026, 7, 6), Decimal("999"), 1, fee, "expired"))
    session.add(_accrual(520, acc.id, date(2026, 7, 7), Decimal("999"), 1, fee, "paid"))
    await session.flush()

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = await service.balance_summary(session, 520, now, date(2026, 7, 20))
    # available_net/gross/commission come ONLY from the 'available' row (50/45/5);
    # effective_rank is separately driven by month gross across ALL states (2047 -> rank 3).
    assert summary.available_gross == Decimal("50")
    assert summary.commission == Decimal("5.0")
    assert summary.available_net == Decimal("45.0")
    assert summary.effective_rank == 3
    assert summary.fee_pct == Decimal("0.03")


async def test_balance_summary_splits_usdt_and_mnt_pools(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_account(session, 526)
    fee = Decimal("0.10")
    # USDT pool: amount 50 -> net 45 (unchanged semantics).
    session.add(_accrual(526, acc.id, date(2026, 7, 5), Decimal("50"), 1, fee, "available"))
    # MNT pool: amount_usdt (USDT-equivalent) 20 -> net_usdt 18; native 100 MNT -> net 90,
    # fx_rate 0.2 (20 USDT == 100 MNT here for the fixture's sake).
    session.add(
        _accrual(
            526,
            acc.id,
            date(2026, 7, 6),
            Decimal("20"),
            1,
            fee,
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            net_native=Decimal("90"),
            fx_rate=Decimal("0.2"),
        )
    )
    await session.flush()

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = await service.balance_summary(session, 526, now, date(2026, 7, 20))

    # USDT-only pool keeps its current meaning: MNT rows must not leak into it.
    assert summary.available_gross == Decimal("50")
    assert summary.commission == Decimal("5.0")
    assert summary.available_net == Decimal("45.0")
    # MNT pool reported separately, in both USDT-equivalent and native units.
    assert summary.mnt_usdt == Decimal("18.0")
    assert summary.mnt_native == Decimal("90")
    assert summary.mnt_withdrawable is True
    # Rank is driven by the COMBINED USDT-equivalent gross (50 + 20 = 70 -> rank 1),
    # not by either pool alone.
    assert summary.effective_rank == 1
    assert summary.withdrawable is True


async def test_balance_summary_active_payout_gates_only_its_own_asset(
    db_session: AsyncSession,
):
    """`balance_summary`'s display flags track `has_active_payout` (reserved/
    pending_admin/submitting/sent) PER ASSET — not shared across pools. A `pending_admin`
    USDT payout flips only `withdrawable`; the independent MNT pool stays withdrawable.
    (Previously a single active payout flipped BOTH — the pre per-asset behavior.)"""
    session = db_session
    acc = await _user_with_account(session, 527)
    fee = Decimal("0.10")
    session.add(_accrual(527, acc.id, date(2026, 7, 5), Decimal("50"), 1, fee, "available"))
    session.add(
        _accrual(
            527,
            acc.id,
            date(2026, 7, 6),
            Decimal("20"),
            1,
            fee,
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            net_native=Decimal("90"),
            fx_rate=Decimal("0.2"),
        )
    )
    await session.flush()
    now = datetime(2026, 7, 20, tzinfo=UTC)

    # A pending_admin USDT payout gates USDT only.
    await _payout(session, 527, "pending_admin", asset="USDT")
    s = await service.balance_summary(session, 527, now, date(2026, 7, 20))
    assert s.withdrawable is False
    assert s.mnt_withdrawable is True  # MNT pool independent -> still withdrawable


async def test_balance_summary_active_mnt_payout_gates_only_mnt(
    db_session: AsyncSession,
):
    """Mirror: a `pending_admin` MNT payout flips only `mnt_withdrawable`; USDT stays free."""
    session = db_session
    acc = await _user_with_account(session, 528)
    fee = Decimal("0.10")
    session.add(_accrual(528, acc.id, date(2026, 7, 5), Decimal("50"), 1, fee, "available"))
    session.add(
        _accrual(
            528,
            acc.id,
            date(2026, 7, 6),
            Decimal("20"),
            1,
            fee,
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            net_native=Decimal("90"),
            fx_rate=Decimal("0.2"),
        )
    )
    await session.flush()
    now = datetime(2026, 7, 20, tzinfo=UTC)

    await _payout(session, 528, "pending_admin", asset="MNT")
    s = await service.balance_summary(session, 528, now, date(2026, 7, 20))
    assert s.withdrawable is True  # USDT pool independent -> still withdrawable
    assert s.mnt_withdrawable is False


async def test_balance_summary_next_withdrawal_at_from_last_success_plus_cooldown(
    db_session: AsyncSession,
):
    session = db_session
    acc = await _user_with_account(session, 521)
    # gross 500 -> rank 2 -> cooldown 14 days.
    session.add(
        _accrual(521, acc.id, date(2026, 7, 5), Decimal("500"), 2, Decimal("0.07"), "available")
    )
    await session.flush()
    await _payout(
        session,
        521,
        "confirmed",
        sent_at=datetime(2026, 7, 1, tzinfo=UTC),
        confirmed_at=datetime(2026, 7, 2, tzinfo=UTC),
    )

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = await service.balance_summary(session, 521, now, date(2026, 7, 20))
    assert summary.effective_rank == 2
    assert summary.next_withdrawal_at == datetime(2026, 7, 16, tzinfo=UTC)
    assert summary.withdrawable is True  # now (7/20) is past cooldown end (7/16)


async def test_balance_summary_withdrawable_false_below_min_payout(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_account(session, 522)
    # gross 10 -> rank 1 (payable), fee 10% -> net 9 < MIN_PAYOUT_USDT (10).
    session.add(
        _accrual(522, acc.id, date(2026, 7, 5), Decimal("10"), 1, Decimal("0.10"), "available")
    )
    await session.flush()

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = await service.balance_summary(session, 522, now, date(2026, 7, 20))
    assert summary.available_net == Decimal("9.0")
    assert summary.withdrawable is False


async def test_balance_summary_withdrawable_false_when_reserved_payout_active(
    db_session: AsyncSession,
):
    session = db_session
    acc = await _user_with_account(session, 523)
    session.add(
        _accrual(523, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()
    await _payout(session, 523, "reserved")

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = await service.balance_summary(session, 523, now, date(2026, 7, 20))
    assert summary.available_net == Decimal("45.0")
    assert summary.withdrawable is False


async def test_balance_summary_nearest_expiry_is_min_of_available(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_account(session, 524)
    session.add(
        _accrual(
            524,
            acc.id,
            date(2026, 7, 5),
            Decimal("50"),
            1,
            Decimal("0.10"),
            "available",
            expires_at=datetime(2026, 8, 4, tzinfo=UTC),
        )
    )
    session.add(
        _accrual(
            524,
            acc.id,
            date(2026, 7, 6),
            Decimal("50"),
            1,
            Decimal("0.10"),
            "available",
            expires_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
    )
    await session.flush()

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = await service.balance_summary(session, 524, now, date(2026, 7, 20))
    assert summary.nearest_expiry == datetime(2026, 8, 4, tzinfo=UTC)


async def test_balance_summary_rank_zero_is_never_withdrawable(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_account(session, 525)
    session.add(_accrual(525, acc.id, date(2026, 7, 5), Decimal("5"), 0, Decimal("0"), "available"))
    await session.flush()

    now = datetime(2026, 7, 20, tzinfo=UTC)
    summary = await service.balance_summary(session, 525, now, date(2026, 7, 20))
    assert summary.effective_rank == 0
    assert summary.fee_pct == Decimal(0)
    assert summary.next_withdrawal_at is None
    assert summary.withdrawable is False


# --- handler ------------------------------------------------------------------


def _message() -> MagicMock:
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.chat = SimpleNamespace(id=1)
    message.message_id = 1
    return message


def _ctx(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _summary(withdrawable: bool) -> service.BalanceSummary:
    return service.BalanceSummary(
        available_gross=Decimal("50"),
        available_net=Decimal("45.0"),
        commission=Decimal("5.0"),
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        next_withdrawal_at=None,
        nearest_expiry=None,
        withdrawable=withdrawable,
    )


def test_render_summary_labels_available_only_when_withdrawable():
    text = stats.render_summary(_summary(True), [], {})
    assert "Доступно к выводу: <b>45.00 USDT</b>" in text


def test_render_summary_marks_unavailable_when_not_withdrawable():
    text = stats.render_summary(_summary(False), [], {})
    # must NOT promise it as withdrawable; shows it as merely accrued + a reason hint
    assert "Доступно к выводу: <b>45.00" not in text
    assert "Накоплено: <b>45.00 USDT</b>" in text
    assert "недоступно к выводу" in text


def test_render_summary_shows_mnt_natively_with_usdt_in_parens():
    # A user whose rebate is (mostly) in MNT sees the MNT amount natively, with the
    # USDT-equivalent in parentheses — both in the balance line and the per-UID line.
    s = service.BalanceSummary(
        available_gross=Decimal("0.01"),
        available_net=Decimal("0.01"),
        commission=Decimal("0"),
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        next_withdrawal_at=None,
        nearest_expiry=None,
        withdrawable=False,
        mnt_native=Decimal("438.95"),
        mnt_usdt=Decimal("194.13"),
        mnt_withdrawable=False,
    )
    acc = SimpleNamespace(id=7, exchange="bybit", uid="580124602")
    assets = {
        7: {
            "MNT": (Decimal("438.95"), Decimal("194.13")),
            "USDT": (Decimal("0.01"), Decimal("0.01")),
        }
    }
    text = stats.render_summary(s, [acc], assets)  # type: ignore[list-item]
    # dedicated MNT balance line (native amount, USDT-equiv in parens)
    assert "Накоплено (MNT): <b>438.95 MNT</b>" in text
    # per-UID line: MNT native first with USDT in parens, then the tiny USDT part
    assert "438.95 MNT (194.13 USDT) + 0.01 USDT" in text


def test_render_summary_shows_last_payout_absent_when_never_withdrew():
    # Default BalanceSummary has last_withdrawal_at=None -> explicit "ещё не было".
    text = stats.render_summary(_summary(True), [], {})
    assert "Последняя выплата: ещё не было" in text


def test_render_summary_shows_last_payout_date_in_payout_tz():
    s = service.BalanceSummary(
        available_gross=Decimal("50"),
        available_net=Decimal("45.0"),
        commission=Decimal("5.0"),
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        next_withdrawal_at=None,
        nearest_expiry=None,
        withdrawable=True,
        last_withdrawal_at=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
    )
    # 06:30 UTC rendered in PAYOUT_TZ (UTC+8) -> 14:30.
    assert "Последняя выплата: 2026-08-10 14:30 (UTC+8)" in stats.render_summary(s, [], {})


def test_render_summary_dual_asset_splits_next_withdrawal_per_asset():
    # A user holding an MNT balance sees per-asset "Ближайшая выплата" lines, because the
    # USDT and MNT cooldowns are independent (here: USDT on cooldown, MNT free now).
    s = service.BalanceSummary(
        available_gross=Decimal("50"),
        available_net=Decimal("45"),
        commission=Decimal("5"),
        effective_rank=1,
        fee_pct=Decimal("0.10"),
        next_withdrawal_at=datetime(2026, 8, 20, 6, 0, tzinfo=UTC),
        nearest_expiry=None,
        withdrawable=False,
        mnt_native=Decimal("100"),
        mnt_usdt=Decimal("50"),
        mnt_withdrawable=True,
        mnt_next_withdrawal_at=None,
    )
    text = stats.render_summary(s, [], {})
    assert "Ближайшая выплата USDT: 2026-08-20 14:00 (UTC+8)" in text
    assert "Ближайшая выплата MNT: доступно сейчас" in text
    # The combined single-line label must NOT appear when the split is shown.
    assert "Ближайшая доступная выплата:" not in text


def test_render_summary_usdt_only_keeps_single_next_withdrawal_line():
    # No MNT balance -> the display is unchanged: one combined "Ближайшая доступная выплата".
    text = stats.render_summary(_summary(True), [], {})
    assert "Ближайшая доступная выплата:" in text
    assert "Ближайшая выплата USDT:" not in text
    assert "Ближайшая выплата MNT:" not in text


def test_render_summary_usdt_only_account_unchanged():
    # A plain USDT account still reads "<amount> USDT" (no coin/parens noise).
    acc = SimpleNamespace(id=3, exchange="gate", uid="U3")
    assets = {3: {"USDT": (Decimal("43.60"), Decimal("43.60"))}}
    text = stats.render_summary(_summary(True), [acc], assets)  # type: ignore[list-item]
    assert "— за месяц 43.60 USDT" in text
    assert "MNT" not in text


async def test_show_stats_renders_balance(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(
        stats, "datetime", SimpleNamespace(now=lambda tz: datetime(2026, 7, 20, tzinfo=UTC))
    )
    session = db_session
    acc = await _user_with_account(session, 530, exchange="gate", uid="U")
    session.add(
        _accrual(530, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    # Expired same-month row: still counts toward account_month_net (any state),
    # but NOT toward available_gross — so the per-UID "за месяц" figure (NET 72.00)
    # must come from a distinct code path than the available-only balance figures.
    session.add(
        _accrual(530, acc.id, date(2026, 7, 6), Decimal("30"), 1, Decimal("0.10"), "expired")
    )
    await session.flush()

    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=530), answer=AsyncMock()
    )
    await stats.show_stats(callback, _ctx(530), session)  # type: ignore[arg-type]

    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()  # panel edited in place
    (args, _kwargs) = message.edit_text.await_args
    text = args[0]
    assert "45.00" in text  # net USDT
    assert "USDT" in text
    assert "gate" in text
    assert f"<code>{acc.uid}</code>" in text
    assert "за месяц" in text
    # NET after the platform fee (45 + 27), NOT gross 80 — never reveal the cut.
    assert "72.00" in text
    assert "80.00" not in text  # gross must not leak
    assert "Комиссия площадки" not in text  # fee no longer surfaced
