from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.accrual import repository as repo
from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.worker import AccrualWorker
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.exchanges.base import CommissionResult, ExchangeAdapter
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_legacy_utc_day_is_not_double_counted_at_millisecond_cutover(pg_engine, monkeypatch):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as session:
        session.add(User(id=9927001, role="user", status="active"))
        await session.flush()
        account = ExchangeAccount(
            user_id=9927001,
            exchange="weex",
            uid="12345",
            status="active",
            accrual_since=date(2026, 10, 6),
        )
        session.add(account)
        await session.flush()
        await repo.upsert_accrual(
            session,
            user_id=9927001,
            account_id=account.id,
            accrual_date=date(2026, 10, 6),
            amount_usdt=Decimal("50"),
            effective_rank=1,
            fee_pct=Decimal("0.1"),
            commission_usdt=Decimal("5"),
            net_usdt=Decimal("45"),
            state="available",
            available_at=datetime(2026, 10, 6, 16, tzinfo=UTC),
            expires_at=datetime(2026, 11, 4, 16, tzinfo=UTC),
            raw_records_count=1,
            adapter_notes="legacy",
        )
        await session.commit()
        account_id = account.id

    active_accounts = repo.active_accounts_by_user

    async def own_accounts(session):
        result = await active_accounts(session)
        return {9927001: result[9927001]}

    monkeypatch.setattr(repo, "active_accounts_by_user", own_accounts)

    class Adapter(ExchangeAdapter):
        name = "WEEX"
        code = "weex"

        def __init__(self):
            self.calls = []

        async def get_commission(self, uid, date_from, date_to):
            self.calls.append((date_from, date_to))
            assert date_from.astimezone(UTC) == datetime(2026, 10, 7, tzinfo=UTC)
            assert date_to.astimezone(UTC) == datetime(2026, 10, 7, 15, 59, 59, 999999, tzinfo=UTC)
            return CommissionResult(
                exchange="WEEX",
                uid=uid,
                date_from=date_from,
                date_to=date_to,
                total_usdt=Decimal("20"),
            )

    adapter = Adapter()
    worker = AccrualWorker(
        sm,
        MappingAdapterProvider({"weex": adapter}),
        settings=make_settings(),
        now=lambda: datetime(2026, 10, 8, 4, tzinfo=UTC),
    )
    await worker.run_cycle()
    assert len(adapter.calls) == 1
    async with sm() as session:
        rows = (
            await session.scalars(
                select(RebateAccrual)
                .where(RebateAccrual.account_id == account_id)
                .order_by(RebateAccrual.accrual_date)
            )
        ).all()
        assert len(rows) == 2
        assert rows[0].amount_usdt == Decimal("50") and rows[0].adapter_notes == "legacy"
        assert rows[1].amount_usdt == Decimal("20")
        assert rows[1].query_from == datetime(2026, 10, 7, tzinfo=UTC)
        assert rows[1].query_to == datetime(2026, 10, 7, 15, 59, 59, 999000, tzinfo=UTC)
        assert await repo.last_weex_query_end(session, account_id) == rows[1].query_to
    await worker.run_cycle()
    assert len(adapter.calls) == 1
