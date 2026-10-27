# tests/admin/test_filter_and_flags.py
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin import repository as admin_repo
from bot.admin.filters import IsAdmin
from bot.admin.keyboards import AdminCB
from bot.admin.router import build_admin_router, open_admin_menu, toggle_killswitch
from bot.botui import repository as repo
from bot.config import Settings
from bot.db.models.identity import User, Wallet
from bot.db.models.payouts import Payout

_async = pytest.mark.asyncio(loop_scope="session")


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("USER_TELEGRAM_IDS", "1")
    monkeypatch.setenv("DEVELOPER_TELEGRAM_ID", "2")


# ---------------------------------------------------------------------------
# IsAdmin — pure, no DB
# ---------------------------------------------------------------------------


@_async
async def test_is_admin_true_for_member() -> None:
    is_admin = IsAdmin(frozenset({5}))
    event = SimpleNamespace(from_user=SimpleNamespace(id=5))
    assert await is_admin(event) is True  # type: ignore[arg-type]


@_async
async def test_is_admin_false_for_non_member() -> None:
    is_admin = IsAdmin(frozenset({5}))
    event = SimpleNamespace(from_user=SimpleNamespace(id=6))
    assert await is_admin(event) is False  # type: ignore[arg-type]


@_async
async def test_is_admin_false_without_from_user() -> None:
    is_admin = IsAdmin(frozenset({5}))
    event = SimpleNamespace(from_user=None)
    assert await is_admin(event) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# config — ADMIN_TELEGRAM_IDS parsing
# ---------------------------------------------------------------------------


def test_admin_telegram_ids_parses_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "5,6")
    assert Settings().admin_telegram_ids == frozenset({5, 6})  # type: ignore[call-arg]


def test_admin_telegram_ids_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    assert Settings().admin_telegram_ids == frozenset()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# repository — flags + pending list (real Postgres)
# ---------------------------------------------------------------------------


@_async
async def test_get_flag_returns_none_when_missing(db_session: AsyncSession) -> None:
    assert await admin_repo.get_flag(db_session, "no_such_flag") is None


@_async
async def test_set_flag_and_get_flag_round_trip(db_session: AsyncSession) -> None:
    await repo.upsert_user(db_session, 9501)

    await admin_repo.set_flag(db_session, "payouts_paused", {"paused": True}, 9501)
    assert await admin_repo.get_flag(db_session, "payouts_paused") == {"paused": True}
    assert await admin_repo.is_payouts_paused(db_session) is True

    await admin_repo.set_flag(db_session, "payouts_paused", {"paused": False}, 9501)
    assert await admin_repo.get_flag(db_session, "payouts_paused") == {"paused": False}
    assert await admin_repo.is_payouts_paused(db_session) is False


@_async
async def test_is_payouts_paused_false_when_flag_absent(db_session: AsyncSession) -> None:
    assert await admin_repo.is_payouts_paused(db_session) is False


async def _user_with_wallet(session: AsyncSession, user_id: int) -> Wallet:
    session.add(User(id=user_id, role="user", status="active"))
    await session.flush()
    wallet = Wallet(
        user_id=user_id,
        address_encrypted=b"enc",
        address_hash=f"hash{user_id}".encode(),
        address_masked="0x1234…abcd",
    )
    session.add(wallet)
    await session.flush()
    return wallet


_payout_seq = 0


async def _payout(session: AsyncSession, user_id: int, wallet_id: int, status: str) -> Payout:
    global _payout_seq
    _payout_seq += 1
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
        idempotency_key=f"idem-{user_id}-{status}-{_payout_seq}",
    )
    session.add(payout)
    await session.flush()
    return payout


@_async
async def test_list_pending_admin_returns_only_pending_admin(db_session: AsyncSession) -> None:
    wallet = await _user_with_wallet(db_session, 9502)
    pending = await _payout(db_session, 9502, wallet.id, "pending_admin")
    await _payout(db_session, 9502, wallet.id, "reserved")
    await _payout(db_session, 9502, wallet.id, "paid")

    result = await admin_repo.list_pending_admin(db_session)

    assert [p.id for p in result] == [pending.id]


@_async
async def test_list_pending_admin_orders_by_requested_at(db_session: AsyncSession) -> None:
    wallet = await _user_with_wallet(db_session, 9503)
    later = await _payout(db_session, 9503, wallet.id, "pending_admin")
    later.requested_at = datetime(2026, 7, 12, tzinfo=UTC)
    earlier = await _payout(db_session, 9503, wallet.id, "pending_admin")
    earlier.requested_at = datetime(2026, 7, 1, tzinfo=UTC)
    await db_session.flush()

    result = await admin_repo.list_pending_admin(db_session)

    assert [p.id for p in result] == [earlier.id, later.id]


# ---------------------------------------------------------------------------
# router — /admin menu + kill-switch toggle (handlers called directly, as in
# tests/botui/test_menu.py / test_accounts_handlers.py)
# ---------------------------------------------------------------------------


def _message() -> Message:
    # Real (frozen) Message so isinstance + panel.remember(msg.chat.id) work; async
    # methods are mocked via object.__setattr__ (the model is frozen).
    msg = Message.model_construct(message_id=1, chat=Chat.model_construct(id=1, type="private"))
    for _name in ("answer", "edit_text", "delete"):
        object.__setattr__(msg, _name, AsyncMock())
    return msg


@_async
async def test_open_admin_menu_answers_with_markup() -> None:
    msg = _message()
    await open_admin_menu(msg, AsyncMock())  # type: ignore[arg-type]
    msg.answer.assert_awaited_once()
    _args, kwargs = msg.answer.call_args
    assert "reply_markup" in kwargs


@_async
async def test_back_to_admin_menu_edits_panel_and_clears_state() -> None:
    from bot.admin.router import back_to_admin_menu

    message = _message()
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=1), answer=AsyncMock())
    state = AsyncMock()  # clear() + panel.remember(update_data) are awaited no-ops here

    await back_to_admin_menu(callback, state)  # type: ignore[arg-type]

    state.clear.assert_awaited()
    message.edit_text.assert_awaited()
    assert "Админ-панель" in message.edit_text.call_args.args[0]
    callback.answer.assert_awaited()


def test_admin_back_keyboard_has_menu_button() -> None:
    from bot.admin.keyboards import admin_back_keyboard

    texts = [b.text for row in admin_back_keyboard().inline_keyboard for b in row]
    assert any("меню админки" in t for t in texts)


@_async
async def test_killswitch_toggle_flips_flag_and_audits(db_session: AsyncSession) -> None:
    await repo.upsert_user(db_session, 9504)
    assert await admin_repo.is_payouts_paused(db_session) is False

    message = _message()
    callback = SimpleNamespace(
        message=message, from_user=SimpleNamespace(id=9504), answer=AsyncMock()
    )
    await toggle_killswitch(callback, AsyncMock(), db_session)  # type: ignore[arg-type]

    assert await admin_repo.is_payouts_paused(db_session) is True
    callback.answer.assert_awaited()
    message.edit_text.assert_awaited()

    # Toggle again -> back to false.
    callback2 = SimpleNamespace(
        message=_message(), from_user=SimpleNamespace(id=9504), answer=AsyncMock()
    )
    await toggle_killswitch(callback2, AsyncMock(), db_session)  # type: ignore[arg-type]
    assert await admin_repo.is_payouts_paused(db_session) is False


@_async
async def test_build_admin_router_returns_router_gated_by_admin_ids() -> None:
    router = build_admin_router(frozenset({9505}))
    assert router.name == "admin"
    # Filter is attached to the router's message/callback_query observers (checked
    # against every handler registered on them, per aiogram's TelegramEventObserver).
    assert len(router.message._handler.filters or []) >= 1
    assert len(router.callback_query._handler.filters or []) >= 1


def test_admin_cb_action_field_roundtrip() -> None:
    cb = AdminCB(action="killswitch")
    assert AdminCB.unpack(cb.pack()).action == "killswitch"
