from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RankHistory, RebateAccrual

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _account(session: AsyncSession, user_id: int, uid: str) -> ExchangeAccount:
    session.add(User(id=user_id, role="user", status="active"))
    await session.flush()
    acc = ExchangeAccount(
        user_id=user_id, exchange="bybit", uid=uid, status="active", accrual_since=date(2026, 7, 10)
    )
    session.add(acc)
    await session.flush()
    return acc


async def test_accrual_round_trip_decimal(db_session: AsyncSession):
    acc = await _account(db_session, 200, "A200")
    db_session.add(
        RebateAccrual(
            user_id=200,
            account_id=acc.id,
            accrual_date=date(2026, 7, 11),
            asset="USDT",
            amount_usdt=Decimal("5.123456789012345678"),
            effective_rank=2,
            fee_pct=Decimal("0.07"),
            commission_usdt=Decimal("0.358641975230864197"),
            net_usdt=Decimal("4.764814813781481481"),
            state="available",
            available_at=datetime(2026, 7, 12, tzinfo=UTC),
            expires_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
    )
    await db_session.flush()


async def test_one_accrual_row_per_account_day(db_session: AsyncSession):
    acc = await _account(db_session, 201, "A201")
    for _ in range(2):
        db_session.add(
            RebateAccrual(
                user_id=201,
                account_id=acc.id,
                accrual_date=date(2026, 7, 11),
                asset="USDT",
                amount_usdt=Decimal("1"),
                effective_rank=1,
                fee_pct=Decimal("0.10"),
                commission_usdt=Decimal("0.10"),
                net_usdt=Decimal("0.90"),
                state="available",
                available_at=datetime(2026, 7, 12, tzinfo=UTC),
                expires_at=datetime(2026, 8, 10, tzinfo=UTC),
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_rank_history_unique_per_month(db_session: AsyncSession):
    db_session.add(User(id=202, role="user", status="active"))
    await db_session.flush()
    db_session.add(
        RankHistory(
            user_id=202,
            period_month=date(2026, 7, 1),
            achieved_rank=2,
            gross_month_usdt=Decimal("150"),
        )
    )
    await db_session.flush()
    db_session.add(
        RankHistory(
            user_id=202,
            period_month=date(2026, 7, 1),
            achieved_rank=3,
            gross_month_usdt=Decimal("1200"),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
