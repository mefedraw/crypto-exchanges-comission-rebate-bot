from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import User, Wallet
from bot.db.models.payouts import Payout

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _payout(user_id: int, wallet_id: int, idem: str, **kw) -> Payout:
    base = {
        "user_id": user_id,
        "wallet_id": wallet_id,
        "address_encrypted": b"a",
        "address_masked": "0x..a",
        "covered_from": date(2026, 7, 1),
        "covered_to": date(2026, 7, 11),
        "gross_usdt": Decimal("100"),
        "commission_usdt": Decimal("7"),
        "gas_usdt": Decimal("0"),
        "net_usdt": Decimal("93"),
        "status": "reserved",
        "idempotency_key": idem,
    }
    base.update(kw)
    return Payout(**base)


async def _user_and_wallet(db_session: AsyncSession, user_id: int) -> int:
    db_session.add(User(id=user_id, role="user", status="active"))
    wallet = Wallet(
        user_id=user_id,
        address_encrypted=b"w",
        address_hash=b"wh",
        address_masked="0x..w",
        status="active",
    )
    db_session.add(wallet)
    await db_session.flush()
    return wallet.id


async def test_payout_round_trip(db_session: AsyncSession):
    wid = await _user_and_wallet(db_session, 300)
    db_session.add(_payout(300, wid, "idem-300"))
    await db_session.flush()


async def test_idempotency_key_unique(db_session: AsyncSession):
    wid = await _user_and_wallet(db_session, 301)
    db_session.add(_payout(301, wid, "dup-key"))
    await db_session.flush()
    db_session.add(_payout(301, wid, "dup-key"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_single_active_nonce(db_session: AsyncSession):
    wid = await _user_and_wallet(db_session, 302)
    db_session.add(
        _payout(
            302,
            wid,
            "n-a",
            status="sent",
            nonce=5,
            txhash="0xaa",
            sent_at=datetime(2026, 7, 12, tzinfo=UTC),
        )
    )
    await db_session.flush()
    # another payout holding the SAME nonce while in an active-send state is forbidden
    db_session.add(_payout(302, wid, "n-b", status="submitting", nonce=5))
    with pytest.raises(IntegrityError):
        await db_session.flush()
