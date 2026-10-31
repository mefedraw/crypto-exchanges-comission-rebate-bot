"""Commit successful API snapshots separately so ledger rollbacks cannot erase them."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models.fetches import CommissionFetch
from bot.exchanges.base import CommissionLine, CommissionResult, ExchangeAdapter

# Bump when adapter interpretation changes; old snapshots must not mask a parser fix.
FORMAT_VERSION = 1
FRESH_FOR = timedelta(hours=24)


class _Line(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    asset: str
    amount: Decimal
    source: str | None


class _Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    exchange: str
    uid: str
    lines: list[_Line]
    total_usdt: Decimal | None
    raw_records_count: int = Field(ge=0)
    notes: list[str]
    settlement_note: str | None


async def get_or_fetch(
    sm: async_sessionmaker[AsyncSession],
    adapter: ExchangeAdapter,
    *,
    account_id: int,
    uid: str,
    day: date,
    query_from: datetime,
    query_to: datetime,
    now: datetime | None = None,
) -> CommissionResult:
    clock = now or datetime.now(UTC)
    if any(value.utcoffset() is None for value in (clock, query_from, query_to)):
        raise ValueError("Commission fetch timestamps must be timezone-aware")
    if query_from > query_to:
        raise ValueError("Commission fetch query bounds are reversed")
    fresh_after = clock - FRESH_FOR
    key = {
        "account_id": account_id,
        "accrual_date": day,
        "query_from": query_from,
        "query_to": query_to,
        "version": FORMAT_VERSION,
    }
    stmt = select(CommissionFetch).filter_by(**key)
    async with sm() as session:
        row = await session.scalar(stmt)
        payload = row.payload if row is not None and row.fetched_at > fresh_after else None
    if payload is None:
        # Only a complete successful adapter response reaches the database.
        result = await adapter.get_commission(uid, query_from, query_to)
        snapshot = _Snapshot(
            exchange=adapter.code,
            uid=uid,
            lines=[
                _Line(asset=line.asset, amount=line.amount, source=line.source)
                for line in result.lines
            ],
            total_usdt=result.total_usdt,
            raw_records_count=result.raw_records_count,
            notes=result.notes,
            settlement_note=result.settlement_note,
        )
        async with sm() as session:
            await session.execute(
                insert(CommissionFetch)
                .values(**key, payload=snapshot.model_dump(mode="json"), fetched_at=clock)
                .on_conflict_do_update(
                    index_elements=list(key),
                    set_={"payload": snapshot.model_dump(mode="json"), "fetched_at": clock},
                    where=CommissionFetch.fetched_at <= fresh_after,
                )
            )
            row = await session.scalar(stmt)
            if row is None:
                raise RuntimeError("Successful commission snapshot was not persisted")
            payload = row.payload
            await session.commit()
    snapshot = _Snapshot.model_validate(payload)
    if snapshot.exchange != adapter.code or snapshot.uid != uid:
        raise ValueError("Commission snapshot account identity changed")
    return CommissionResult(
        exchange=adapter.name,
        uid=uid,
        date_from=query_from,
        date_to=query_to,
        lines=[
            CommissionLine(asset=line.asset, amount=line.amount, source=line.source)
            for line in snapshot.lines
        ],
        total_usdt=snapshot.total_usdt,
        raw_records_count=snapshot.raw_records_count,
        notes=snapshot.notes,
        settlement_note=snapshot.settlement_note,
    )
