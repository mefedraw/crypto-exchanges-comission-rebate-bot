# tests/notifications/test_poller.py
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models.ops import Notification
from bot.notifications import poller
from bot.notifications import repository as repo

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _now() -> datetime:
    return NOW


# ---------------------------------------------------------------- format_message
def test_format_payout_paid_has_amount_and_mainnet_link():
    n = Notification(
        kind=repo.PAYOUT_PAID, payload={"net_usdt": "14.64", "txhash": "0xabc"}, recipient_chat_id=1
    )
    text = poller.format_message(n, chain_id=56)
    assert "дошли до получателя" in text
    assert "14.64 USDT" in text
    assert "https://bscscan.com/tx/0xabc" in text


def test_format_payout_paid_uses_testnet_explorer():
    n = Notification(
        kind=repo.PAYOUT_PAID, payload={"net_usdt": "1", "txhash": "0xd"}, recipient_chat_id=1
    )
    assert "https://testnet.bscscan.com/tx/0xd" in poller.format_message(n, chain_id=97)


def test_format_payout_paid_shows_gas_deduction():
    n = Notification(
        kind=repo.PAYOUT_PAID,
        payload={"net_usdt": "14.56", "gas_usdt": "0.072", "txhash": "0xa"},
        recipient_chat_id=1,
    )
    text = poller.format_message(n, chain_id=56)
    assert "14.56 USDT" in text
    assert "0.0720" in text and "газ" in text


def test_format_payout_failed():
    n = Notification(kind=repo.PAYOUT_FAILED, payload={"net_usdt": "5"}, recipient_chat_id=1)
    text = poller.format_message(n, chain_id=56)
    assert "Не удалось" in text and "5.00 USDT" in text  # 2dp formatting


# ---------------------------------------------------------- FINDING 2: MNT notifications
def test_format_payout_paid_mnt_shows_native_amount_and_mnt_unit():
    """An MNT payout's arrival notification must show the native MNT amount (not the
    USDT-equivalent `net_usdt`) with the "MNT" unit — never "USDT"."""
    n = Notification(
        kind=repo.PAYOUT_PAID,
        payload={
            "asset": "MNT",
            "net_usdt": "60",  # USD-equivalent — must NOT be what's rendered
            "net_native": "100.5",
            "txhash": "0xabc",
        },
        recipient_chat_id=1,
    )
    text = poller.format_message(n, chain_id=56)
    assert "100.50 MNT" in text
    assert "USDT" not in text


def test_format_payout_paid_mnt_floors_to_the_amount_actually_sent():
    """FIX M1: `native_to_wei` floors the MNT amount to whole cents (ROUND_DOWN)
    before broadcasting, so the PAID notification must show that same floored
    figure — not a ROUND_HALF_UP rounding of the raw (unfloored) `net_native`,
    which could tell the user a HIGHER amount than was actually sent
    (12.345 -> "12.35 MNT" would be wrong; 12.34 MNT is what was sent)."""
    n = Notification(
        kind=repo.PAYOUT_PAID,
        payload={
            "asset": "MNT",
            "net_usdt": "60",
            "net_native": "12.345",
            "txhash": "0xabc",
        },
        recipient_chat_id=1,
    )
    text = poller.format_message(n, chain_id=56)
    assert "12.34 MNT" in text
    assert "12.35 MNT" not in text


def test_format_payout_failed_mnt_shows_native_amount_and_mnt_unit():
    n = Notification(
        kind=repo.PAYOUT_FAILED,
        payload={"asset": "MNT", "net_usdt": "60", "net_native": "100.5", "reason": "reverted"},
        recipient_chat_id=1,
    )
    text = poller.format_message(n, chain_id=56)
    assert "100.50 MNT" in text
    assert "USDT" not in text


def test_format_payout_paid_usdt_unchanged_when_asset_present():
    """Adding the `asset` field must not change existing USDT wording/amount."""
    n = Notification(
        kind=repo.PAYOUT_PAID,
        payload={"asset": "USDT", "net_usdt": "14.64", "net_native": "14.64", "txhash": "0xabc"},
        recipient_chat_id=1,
    )
    text = poller.format_message(n, chain_id=56)
    assert "14.64 USDT" in text


def test_format_dev_alert():
    n = Notification(
        kind=repo.DEV_ALERT, payload={"title": "Boom", "detail": "payout #3"}, recipient_chat_id=1
    )
    text = poller.format_message(n, chain_id=56)
    assert "Boom" in text and "payout #3" in text


# ---------------------------------------------------------------- deliver_pending
async def test_deliver_sends_and_marks_delivered(
    sessionmaker: async_sessionmaker[AsyncSession],
):
    async with sessionmaker() as s:
        await repo.enqueue(
            s, recipient_chat_id=555, kind=repo.PAYOUT_PAID, payload={"net_usdt": "9"}
        )
        await s.commit()

    bot = AsyncMock()
    sent = await poller.deliver_pending(bot, sessionmaker, chain_id=56, now=_now)

    assert sent == 1
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 555  # recipient chat id
    # nothing left to deliver
    async with sessionmaker() as s:
        assert await repo.claim_undelivered(s) == []


async def test_failed_send_bumps_attempt_and_retries(
    sessionmaker: async_sessionmaker[AsyncSession],
):
    async with sessionmaker() as s:
        await repo.enqueue(
            s, recipient_chat_id=666, kind=repo.PAYOUT_PAID, payload={"net_usdt": "9"}
        )
        await s.commit()

    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("blocked by user")
    sent = await poller.deliver_pending(bot, sessionmaker, chain_id=56, now=_now)

    assert sent == 0
    # still undelivered, attempts bumped -> visible + retriable
    async with sessionmaker() as s:
        rows = await repo.claim_undelivered(s)
        assert len(rows) == 1 and rows[0].attempts == 1
