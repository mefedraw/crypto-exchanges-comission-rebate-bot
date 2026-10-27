# tests/botui/test_withdraw.py
from __future__ import annotations

import uuid
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

from bot.botui import menu, service, withdraw
from bot.botui import repository as repo
from bot.botui.keyboards import WithdrawConfirmCB, WithdrawStartCB
from bot.botui.states import WithdrawFlow
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import ActionToken
from bot.db.models.payouts import Payout
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 7, 20, tzinfo=UTC)
TODAY = date(2026, 7, 20)


@pytest.fixture(autouse=True)
def _mnt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """FIX M3 gates MNT withdrawal (button + `prepare_withdrawal`) on
    `settings.mnt_enabled`. This module's pre-existing scenarios exercise the MNT
    path expecting it to be reachable, so default the flag ON here; the
    disabled-flag scenario itself (see `test_prepare_withdrawal_blocked_when_mnt_disabled`
    and `test_cabinet_menu_hides_mnt_button_when_disabled_even_with_nonempty_pool`
    below) overrides it back to OFF locally."""
    settings = make_settings(mantle_rpc_url="https://rpc.mantle.xyz")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(menu, "get_settings", lambda: settings)
    monkeypatch.setattr(withdraw, "get_settings", lambda: settings)


async def _user_with_wallet(
    session: AsyncSession, user_id: int, exchange: str = "bybit", uid: str = "U"
) -> ExchangeAccount:
    session.add(User(id=user_id, role="user", status="active"))
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
    asset: str = "USDT",
    amount_native: Decimal | None = None,
    fx_rate: Decimal = Decimal("1"),
) -> RebateAccrual:
    """``amount`` is always the USDT-equivalent (``amount_usdt``/frozen gross).

    For MNT rows pass ``asset="MNT"`` plus ``amount_native``/``fx_rate``; the
    native fields default to mirroring ``amount`` (fx=1), i.e. plain USDT."""
    commission = amount * fee
    native = amount if amount_native is None else amount_native
    commission_native = native * fee
    return RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=d,
        asset=asset,
        amount_usdt=amount,
        effective_rank=rank,
        fee_pct=fee,
        commission_usdt=commission,
        net_usdt=amount - commission,
        amount_native=native,
        net_native=native - commission_native,
        fx_rate=fx_rate,
        state=state,
        available_at=datetime(2026, 7, 12, tzinfo=UTC),
        expires_at=expires_at,
        raw_records_count=1,
        adapter_notes=None,
    )


async def _payout(
    session: AsyncSession,
    user_id: int,
    wallet_id: int,
    status: str,
    *,
    sent_at: datetime | None = None,
    confirmed_at: datetime | None = None,
) -> Payout:
    payout = Payout(
        user_id=user_id,
        wallet_id=wallet_id,
        address_encrypted=b"x",
        address_masked="0x..abcd",
        covered_from=date(2026, 7, 1),
        covered_to=date(2026, 7, 31),
        gross_usdt=Decimal("10"),
        commission_usdt=Decimal("1"),
        net_usdt=Decimal("9"),
        status=status,
        sent_at=sent_at,
        confirmed_at=confirmed_at,
        idempotency_key=f"idem-{user_id}-{status}-{sent_at}-{confirmed_at}",
    )
    session.add(payout)
    await session.flush()
    return payout


# --- service.prepare_withdrawal -----------------------------------------------


async def test_blocked_no_wallet(db_session: AsyncSession):
    session = db_session
    session.add(User(id=600, role="user", status="active"))
    await session.flush()
    result = await service.prepare_withdrawal(session, 600, NOW, TODAY)
    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "no_wallet"


async def test_blocked_below_min(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_wallet(session, 601)
    # gross 10 -> rank 1 (payable), fee 10% -> net 9 < MIN_PAYOUT_USDT (10).
    session.add(
        _accrual(601, acc.id, date(2026, 7, 5), Decimal("10"), 1, Decimal("0.10"), "available")
    )
    await session.flush()

    result = await service.prepare_withdrawal(session, 601, NOW, TODAY)
    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "below_min"


async def test_blocked_when_reserved_exists(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_wallet(session, 602)
    session.add(
        _accrual(602, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()
    wallet = await repo.get_active_wallet(session, 602)
    assert wallet is not None
    await _payout(session, 602, wallet.id, "reserved")

    result = await service.prepare_withdrawal(session, 602, NOW, TODAY)
    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "pending"


async def test_blocked_cooldown(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_wallet(session, 603)
    # gross 50 -> rank 1 -> cooldown 30 days.
    session.add(
        _accrual(603, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()
    wallet = await repo.get_active_wallet(session, 603)
    assert wallet is not None
    await _payout(
        session,
        603,
        wallet.id,
        "confirmed",
        sent_at=datetime(2026, 7, 18, tzinfo=UTC),
        confirmed_at=datetime(2026, 7, 19, tzinfo=UTC),
    )

    result = await service.prepare_withdrawal(session, 603, NOW, TODAY)
    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "cooldown"
    assert result.detail is not None


async def test_prepare_withdrawal_returns_preview_when_eligible(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_wallet(session, 604)
    session.add(
        _accrual(604, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()

    result = await service.prepare_withdrawal(session, 604, NOW, TODAY)
    assert isinstance(result, service.WithdrawPreview)
    assert result.gross == Decimal("50")
    assert result.commission == Decimal("5.0")
    assert result.net == Decimal("45.0")
    assert result.wallet_masked == "0x1234…abcd"


# --- has_active_payout: shared single-active-request across BOTH assets -------


async def test_has_active_payout_true_for_pending_admin(db_session: AsyncSession):
    """`has_active_payout` must catch `pending_admin`, not just `reserved` — an
    MNT payout (always admin-gated) still counts as an active request."""
    session = db_session
    acc = await _user_with_wallet(session, 640)
    session.add(
        _accrual(640, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()
    wallet = await repo.get_active_wallet(session, 640)
    assert wallet is not None

    assert await repo.has_active_payout(session, 640) is False
    await _payout(session, 640, wallet.id, "pending_admin")
    assert await repo.has_active_payout(session, 640) is True


# --- double-spend: the critical money test ------------------------------------


async def test_double_confirm_creates_one_payout(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_wallet(session, 605)
    session.add(
        _accrual(605, acc.id, date(2026, 7, 5), Decimal("30"), 1, Decimal("0.10"), "available")
    )
    session.add(
        _accrual(605, acc.id, date(2026, 7, 6), Decimal("30"), 1, Decimal("0.10"), "available")
    )
    await session.flush()

    preview = await service.prepare_withdrawal(session, 605, NOW, TODAY)
    assert isinstance(preview, service.WithdrawPreview)
    token = await service.issue_withdraw_token(session, 605, preview, NOW)

    p1 = await service.confirm_withdrawal(session, 605, token, NOW)
    p2 = await service.confirm_withdrawal(session, 605, token, NOW)

    assert p1 is not None
    assert p2 is None

    payouts = (await session.scalars(select(Payout).where(Payout.user_id == 605))).all()
    assert len(payouts) == 1
    assert payouts[0].status == "reserved"
    assert payouts[0].id == p1.id

    accruals = (
        await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 605))
    ).all()
    assert len(accruals) == 2
    for a in accruals:
        assert a.state == "reserved"
        assert a.payout_id == payouts[0].id


async def test_confirm_withdrawal_returns_none_on_expired_token(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_wallet(session, 606)
    session.add(
        _accrual(606, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()

    preview = await service.prepare_withdrawal(session, 606, NOW, TODAY)
    assert isinstance(preview, service.WithdrawPreview)
    stale_token = uuid.uuid4()
    await repo.create_action_token(
        session,
        token=stale_token,
        user_id=606,
        action_type="withdraw",
        payload={"accrual_ids": preview.accrual_ids, "gross": "50", "commission": "5", "net": "45"},
        expires_at=NOW,  # already expired at NOW (expires_at > now required)
    )
    payout = await service.confirm_withdrawal(session, 606, stale_token, NOW)
    assert payout is None


async def test_confirm_withdrawal_mnt_creates_pending_admin_payout(db_session: AsyncSession):
    """MNT is ALWAYS admin-gated: confirm must open the Payout as `pending_admin`
    (not `reserved`) with the correct native MNT amount, well below any USDT
    auto-limit — unlike USDT, there is no auto-send path for MNT at all."""
    session = db_session
    acc = await _user_with_wallet(session, 620)
    session.add(
        _accrual(
            620,
            acc.id,
            date(2026, 7, 5),
            Decimal("60"),
            1,
            Decimal("0.10"),
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            fx_rate=Decimal("0.6"),
        )
    )
    await session.flush()

    preview = await service.prepare_withdrawal(session, 620, NOW, TODAY, asset="MNT")
    assert isinstance(preview, service.WithdrawPreview)
    assert preview.asset == "MNT"
    assert preview.net == Decimal("54.0")  # USDT-equiv: 60 - 60*0.10
    assert preview.net_native == Decimal("90.0")  # native MNT: 100 - 100*0.10

    token = await service.issue_withdraw_token(session, 620, preview, NOW)
    payout = await service.confirm_withdrawal(session, 620, token, NOW)

    assert isinstance(payout, Payout)
    assert payout.asset == "MNT"
    assert payout.status == "pending_admin"
    assert payout.net_usdt == Decimal("54.0")
    assert payout.net_native == Decimal("90.0")


async def test_confirm_withdrawal_other_asset_allowed_during_token_window(
    db_session: AsyncSession,
):
    """Per-asset independence: a user who calls prepare once per asset within the 5-min
    token TTL holds two live tokens. Confirming BOTH must now succeed — one active USDT
    payout and one active MNT payout coexist (the pools are independent). This is the
    new intended behavior; previously the second (MNT) confirm was blocked."""
    session = db_session
    acc = await _user_with_wallet(session, 650)
    session.add(
        _accrual(650, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    session.add(
        _accrual(
            650,
            acc.id,
            date(2026, 7, 6),
            Decimal("60"),
            1,
            Decimal("0.10"),
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            fx_rate=Decimal("0.6"),
        )
    )
    await session.flush()

    preview_usdt = await service.prepare_withdrawal(session, 650, NOW, TODAY, asset="USDT")
    assert isinstance(preview_usdt, service.WithdrawPreview)
    token_usdt = await service.issue_withdraw_token(session, 650, preview_usdt, NOW)

    preview_mnt = await service.prepare_withdrawal(session, 650, NOW, TODAY, asset="MNT")
    assert isinstance(preview_mnt, service.WithdrawPreview)
    token_mnt = await service.issue_withdraw_token(session, 650, preview_mnt, NOW)

    payout1 = await service.confirm_withdrawal(session, 650, token_usdt, NOW)
    assert isinstance(payout1, Payout)
    # The MNT confirm now SUCCEEDS — a different pool, not blocked by the active USDT one.
    payout2 = await service.confirm_withdrawal(session, 650, token_mnt, NOW)
    assert isinstance(payout2, Payout)

    payouts = (await session.scalars(select(Payout).where(Payout.user_id == 650))).all()
    assert len(payouts) == 2
    assert {p.asset for p in payouts} == {"USDT", "MNT"}


async def test_confirm_withdrawal_same_asset_second_blocked_during_token_window(
    db_session: AsyncSession,
):
    """Same-asset guard is preserved: two USDT tokens issued in one window must NOT both
    confirm. The first reserves a payout; the second is blocked by the per-asset
    has_active_payout re-check and is NOT consumed (a later retry still works)."""
    session = db_session
    acc = await _user_with_wallet(session, 655)
    session.add(
        _accrual(655, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()

    preview1 = await service.prepare_withdrawal(session, 655, NOW, TODAY, asset="USDT")
    assert isinstance(preview1, service.WithdrawPreview)
    token1 = await service.issue_withdraw_token(session, 655, preview1, NOW)
    # Second prepare races before either confirms -> also a valid preview/token.
    preview2 = await service.prepare_withdrawal(session, 655, NOW, TODAY, asset="USDT")
    assert isinstance(preview2, service.WithdrawPreview)
    token2 = await service.issue_withdraw_token(session, 655, preview2, NOW)

    payout1 = await service.confirm_withdrawal(session, 655, token1, NOW)
    assert isinstance(payout1, Payout)

    result2 = await service.confirm_withdrawal(session, 655, token2, NOW)
    assert isinstance(result2, service.WithdrawBlocked)
    assert result2.reason == "pending"

    payouts = (await session.scalars(select(Payout).where(Payout.user_id == 655))).all()
    assert len(payouts) == 1

    # token2 was NOT consumed by the block -- still "issued" for a later retry.
    token_row = await session.get(ActionToken, token2)
    assert token_row is not None
    assert token_row.state == "issued"


async def test_prepare_withdrawal_rejects_invalid_asset(db_session: AsyncSession):
    session = db_session
    session.add(User(id=651, role="user", status="active"))
    await session.flush()

    with pytest.raises(ValueError):
        await service.prepare_withdrawal(session, 651, NOW, TODAY, asset="BTC")


async def test_prepare_withdrawal_blocked_when_mnt_disabled(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX M3: if MANTLE_RPC_URL is unset (mnt_enabled=False) after MNT accruals
    already exist, prepare_withdrawal must refuse to create a payout for the MNT
    pool rather than let it fail closed later in the executor."""
    disabled = make_settings()  # mnt_enabled defaults to False (no MANTLE_RPC_URL)
    monkeypatch.setattr(service, "get_settings", lambda: disabled)
    session = db_session
    acc = await _user_with_wallet(session, 660)
    session.add(
        _accrual(
            660,
            acc.id,
            date(2026, 7, 5),
            Decimal("60"),
            1,
            Decimal("0.10"),
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            fx_rate=Decimal("0.6"),
        )
    )
    await session.flush()

    result = await service.prepare_withdrawal(session, 660, NOW, TODAY, asset="MNT")

    assert isinstance(result, service.WithdrawBlocked)
    assert result.reason == "mnt_disabled"


# --- handlers ------------------------------------------------------------------


def _ctx(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _message() -> MagicMock:
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.chat = SimpleNamespace(id=1)
    message.message_id = 1
    return message


def test_last_payout_line_absent_when_none():
    assert withdraw._last_payout_line(None) == "Последняя выплата: ещё не было"


def test_last_payout_line_formats_in_payout_tz():
    # 06:30 UTC -> 14:30 in PAYOUT_TZ (UTC+8).
    dt = datetime(2026, 8, 10, 6, 30, tzinfo=UTC)
    assert withdraw._last_payout_line(dt) == "Последняя выплата: 2026-08-10 14:30 (UTC+8)"


async def test_start_withdraw_blocked_reports_reason(db_session: AsyncSession):
    session = db_session
    session.add(User(id=610, role="user", status="active"))
    await session.flush()
    state = _ctx(610)
    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=610), answer=AsyncMock()
    )
    await withdraw.start_withdraw(callback, WithdrawStartCB(asset="USDT"), state, session)  # type: ignore[arg-type]
    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()
    assert await state.get_state() is None
    # The blocked screen also surfaces the last-payout date (absent here).
    (args, _kwargs) = message.edit_text.await_args
    assert "Последняя выплата: ещё не было" in args[0]


async def test_start_withdraw_eligible_issues_token_and_sets_state(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(withdraw, "datetime", SimpleNamespace(now=lambda tz: NOW))
    session = db_session
    acc = await _user_with_wallet(session, 611)
    session.add(
        _accrual(611, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()
    state = _ctx(611)
    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=611), answer=AsyncMock()
    )
    await withdraw.start_withdraw(callback, WithdrawStartCB(asset="USDT"), state, session)  # type: ignore[arg-type]
    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()
    assert await state.get_state() == WithdrawFlow.confirming.state
    (args, _kwargs) = message.edit_text.await_args
    assert "USDT" in args[0]


async def test_start_withdraw_mnt_prepares_mnt_asset_and_shows_admin_note(
    db_session: AsyncSession,
    monkeypatch,
):
    """Tapping the MNT withdraw button must prepare the MNT pool (not USDT) and
    the preview must call out that MNT payouts need admin confirmation."""
    monkeypatch.setattr(withdraw, "datetime", SimpleNamespace(now=lambda tz: NOW))
    session = db_session
    acc = await _user_with_wallet(session, 660)
    session.add(
        _accrual(
            660,
            acc.id,
            date(2026, 7, 5),
            Decimal("60"),
            1,
            Decimal("0.10"),
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            fx_rate=Decimal("0.6"),
        )
    )
    await session.flush()
    state = _ctx(660)
    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=660), answer=AsyncMock()
    )
    await withdraw.start_withdraw(callback, WithdrawStartCB(asset="MNT"), state, session)  # type: ignore[arg-type]
    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()
    (args, _kwargs) = message.edit_text.await_args
    assert "MNT" in args[0]
    assert "после подтверждения администратором" in args[0]
    assert await state.get_state() == WithdrawFlow.confirming.state


# --- cabinet menu: MNT button gated on a non-empty MNT pool --------------------


async def test_cabinet_menu_hides_mnt_button_when_pool_empty(db_session: AsyncSession):
    session = db_session
    await repo.upsert_user(session, 661)
    state = _ctx(661)
    callback = SimpleNamespace(
        message=_message(), from_user=SimpleNamespace(id=661), answer=AsyncMock()
    )
    await menu.back_to_menu(callback, state, session)  # type: ignore[arg-type]
    (_args, kwargs) = callback.message.edit_text.await_args
    kb = kwargs["reply_markup"]
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("USDT" in t for t in texts)
    assert not any("MNT" in t for t in texts)


async def test_cabinet_menu_shows_mnt_button_when_pool_nonempty(db_session: AsyncSession):
    session = db_session
    acc = await _user_with_wallet(session, 662)
    session.add(
        _accrual(
            662,
            acc.id,
            date(2026, 7, 5),
            Decimal("60"),
            1,
            Decimal("0.10"),
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            fx_rate=Decimal("0.6"),
        )
    )
    await session.flush()
    state = _ctx(662)
    callback = SimpleNamespace(
        message=_message(), from_user=SimpleNamespace(id=662), answer=AsyncMock()
    )
    await menu.back_to_menu(callback, state, session)  # type: ignore[arg-type]
    (_args, kwargs) = callback.message.edit_text.await_args
    kb = kwargs["reply_markup"]
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Вывести MNT" in t for t in texts)


async def test_cabinet_menu_hides_mnt_button_when_disabled_even_with_nonempty_pool(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX M3: a residual MNT balance alone must not be enough to show the button —
    the feature flag (settings.mnt_enabled) must ALSO be on."""
    disabled = make_settings()  # mnt_enabled defaults to False (no MANTLE_RPC_URL)
    monkeypatch.setattr(menu, "get_settings", lambda: disabled)
    session = db_session
    acc = await _user_with_wallet(session, 663)
    session.add(
        _accrual(
            663,
            acc.id,
            date(2026, 7, 5),
            Decimal("60"),
            1,
            Decimal("0.10"),
            "available",
            asset="MNT",
            amount_native=Decimal("100"),
            fx_rate=Decimal("0.6"),
        )
    )
    await session.flush()
    state = _ctx(663)
    callback = SimpleNamespace(
        message=_message(), from_user=SimpleNamespace(id=663), answer=AsyncMock()
    )
    await menu.back_to_menu(callback, state, session)  # type: ignore[arg-type]
    (_args, kwargs) = callback.message.edit_text.await_args
    kb = kwargs["reply_markup"]
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("USDT" in t for t in texts)
    assert not any("MNT" in t for t in texts)


async def test_confirm_withdraw_handler_success(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    session = db_session
    acc = await _user_with_wallet(session, 612)
    session.add(
        _accrual(612, acc.id, date(2026, 7, 5), Decimal("50"), 1, Decimal("0.10"), "available")
    )
    await session.flush()
    preview = await service.prepare_withdrawal(session, 612, NOW, TODAY)
    assert isinstance(preview, service.WithdrawPreview)
    token = await service.issue_withdraw_token(session, 612, preview, NOW)

    # The handler reads the wall clock for the token's 5-minute TTL; pin it to NOW so a
    # just-issued token isn't judged expired when the suite runs at a real time far from
    # the fixed test NOW (this test is otherwise time-flaky).
    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return NOW

    monkeypatch.setattr(withdraw, "datetime", _FixedNow)

    state = _ctx(612)
    await state.set_state(WithdrawFlow.confirming)
    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=612), answer=AsyncMock()
    )
    await withdraw.confirm_withdraw(callback, WithdrawConfirmCB(token=str(token)), state, session)  # type: ignore[arg-type]
    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()
    (args, _kwargs) = message.edit_text.await_args
    assert "✅" in args[0]
    assert await state.get_state() is None


async def test_confirm_withdraw_handler_stale_token_reports_soft_message(db_session: AsyncSession):
    session = db_session
    await repo.upsert_user(session, 613)
    stale_token = uuid.uuid4()  # never issued -> consume_action_token returns None

    state = _ctx(613)
    await state.set_state(WithdrawFlow.confirming)
    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=613), answer=AsyncMock()
    )
    await withdraw.confirm_withdraw(
        callback, WithdrawConfirmCB(token=str(stale_token)), state, session
    )  # type: ignore[arg-type]
    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()
    (args, _kwargs) = message.edit_text.await_args
    assert "истёк" in args[0] or "обрабатывается" in args[0]
    assert await state.get_state() is None
