"""Reconcile a closed Binance cutover, then post only the reviewed missing days."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import defaultdict
from dataclasses import astuple, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.accrual import repository as repo
from bot.accrual.worker import _day_bounds_payout_tz
from bot.config import RETIRED_EXCHANGES, get_settings
from bot.db.engine import make_engine, make_sessionmaker
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.invites import InviteAttribution
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent
from bot.db.settings import DbSettings
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.binance import POLICY, BinanceAdapter
from bot.exchanges.registry import build_adapters
from bot.payout.commission import accrual_fee
from bot.payout.expiry import LIFETIME_DAYS, PAYOUT_TZ
from bot.payout.rank_timing import (
    day_effective_rank,
    iter_days,
    month_start,
    next_month_start,
    prev_month_start,
)
from bot.payout.ranks import rank_for_gross

ZERO = Decimal(0)
NOTE = POLICY + " Historical reconciliation."


@dataclass(frozen=True, order=True)
class Target:
    owner: int
    account: int
    uid: str
    since: date
    lag: int


@dataclass(frozen=True)
class Day:
    gross: Decimal
    count: int


@dataclass(frozen=True)
class Entry:
    target: Target
    day: date
    source: Day
    rank: int
    fee: Decimal
    net: Decimal
    exists: bool


async def targets(s: AsyncSession, adapters: dict[str, ExchangeAdapter]) -> list[Target]:
    cfg = get_settings()
    found = []
    for owner, accounts in (await repo.active_accounts_by_user(s)).items():
        for acc in accounts:
            if acc.exchange != "binance":
                continue
            codes = {a.exchange for a in accounts} - cfg.disabled_exchanges - RETIRED_EXCHANGES
            lag = max(adapters[code].settlement_lag_days for code in codes)
            found.append(
                Target(owner, acc.id, acc.uid, max(cfg.rebate_epoch, acc.accrual_since), lag)
            )
    return sorted(found)


async def source_days(adapter: BinanceAdapter, target: Target, through: date) -> dict[date, Day]:
    start = await adapter.accrual_start(target.uid, target.since)
    if start is None or start > through:
        return {}

    async def fetch(lo: date, hi: date) -> Day:
        result = await adapter.get_commission(
            target.uid, _day_bounds_payout_tz(lo)[0], _day_bounds_payout_tz(hi)[1]
        )
        if (
            len(result.lines) != 1
            or result.lines[0].asset != "USDT"
            or result.settlement_note != POLICY
        ):
            raise ValueError("Unexpected Binance denomination or policy")
        return Day(result.lines[0].amount, result.raw_records_count)

    days = {}
    while start <= through:
        end = min(
            through,
            next_month_start(month_start(start)) - timedelta(days=1),
            start + timedelta(days=adapter.max_window_days - 1),
        )
        control = await fetch(start, end)
        subtotal = ZERO
        for d in iter_days(start, end):
            days[d] = await fetch(d, d)
            subtotal += days[d].gross
        if subtotal != control.gross:
            raise ValueError(
                f"Binance daily sum disagrees with period {start}..{end}; nothing posted"
            )
        start = end + timedelta(days=1)
    return days


async def plan_owner(
    s: AsyncSession, owner: int, sources: dict[Target, dict[date, Day]], through: date
) -> tuple[list[Entry], dict[date, tuple[int, Decimal]]]:
    owner_sources = {t: ds for t, ds in sources.items() if t.owner == owner}
    first = min((d for ds in owner_sources.values() for d in ds), default=None)
    if first is None:
        return [], {}
    if await s.scalar(select(User.status).where(User.id == owner)) != "active":
        raise ValueError("Inactive user requires a separate review")
    account_ids = {t.account for t in owner_sources}
    override, since = await repo.get_commission_override(s, owner)
    history = await s.execute(
        select(RebateAccrual, ExchangeAccount.exchange)
        .join(ExchangeAccount, ExchangeAccount.id == RebateAccrual.account_id)
        .where(RebateAccrual.user_id == owner)
    )
    saved: dict[tuple[int, date], RebateAccrual] = {}
    siblings: dict[date, list[tuple[RebateAccrual, str]]] = defaultdict(list)
    for row, exchange in history:
        d = row.accrual_date
        if row.account_id in account_ids:
            if d > through and row.correction_key is None:
                continue
            if row.correction_key is not None or row.asset != "USDT" or not row.finalized:
                raise ValueError(
                    "Existing Binance correction or ambiguous row; manual review required"
                )
            saved[row.account_id, d] = row
        elif d >= month_start(first):
            siblings[d].append((row, exchange))
    expected = {(t.account, d) for t, ds in owner_sources.items() for d in ds}
    if saved.keys() - expected:
        raise ValueError("Existing Binance rows outside confirmed source coverage")
    entries = []
    months: dict[date, tuple[int, Decimal]] = {}
    current = month_start(first)
    cumulative = ZERO
    carry1 = await repo.get_achieved_rank(s, owner, prev_month_start(current)) or 0
    carry2 = (
        await repo.get_achieved_rank(s, owner, prev_month_start(prev_month_start(current))) or 0
    )
    for d in iter_days(current, through):
        source_today = [(t, ds[d]) for t, ds in owner_sources.items() if d in ds]
        gross = sum((r.amount_usdt for r, _ in siblings[d]), ZERO) + sum(
            (v.gross for _, v in source_today), ZERO
        )
        rank = day_effective_rank(cumulative, gross, carry1, carry2)
        day_override = override if since and d >= since else None
        fee = accrual_fee(rank, day_override, "binance", d)
        for row, exchange in siblings[d]:
            if (
                d >= first
                and row.amount_usdt > 0
                and row.correction_key is None
                and row.fee_pct != accrual_fee(rank, day_override, exchange, d)
            ):
                raise ValueError(f"Sibling fee requires its own reconciliation on {d}")
        for target, value in source_today:
            net = value.gross - value.gross * fee
            prior = saved.get((target.account, d))
            if prior is not None and (
                prior.adapter_notes not in {POLICY, NOTE}
                or prior.amount_usdt != value.gross
                or prior.net_usdt != net
                or prior.fee_pct != fee
                or prior.effective_rank != rank
                or prior.net_native != net
                or prior.amount_native != value.gross
                or prior.commission_usdt != value.gross - net
                or prior.fx_rate != 1
            ):
                raise ValueError(f"Frozen Binance row differs on {d}; nothing overwritten")
            entries.append(Entry(target, d, value, rank, fee, net, prior is not None))
        cumulative += gross
        if (d + timedelta(days=1)).day == 1:
            achieved = max(
                rank_for_gross(cumulative), await repo.get_achieved_rank(s, owner, current) or 0
            )
            months[current] = achieved, cumulative
            current, carry2, carry1, cumulative = d + timedelta(days=1), carry1, achieved, ZERO
    if any(e.source.gross and not e.exists for e in entries):
        if any(d > through and any(r.amount_usdt for r, _ in rs) for d, rs in siblings.items()):
            raise ValueError("Positive sibling rows after cutoff require separate fee review")
        if await s.get(InviteAttribution, owner) is not None:
            raise ValueError("Historical partner share requires a separate review")
    return entries, months


async def reconcile(
    s: AsyncSession,
    sources: dict[Target, dict[date, Day]],
    through: date,
    now: datetime,
    apply_digest: str | None = None,
) -> dict[str, object]:
    entries: list[Entry] = []
    months = {}
    for owner in sorted({t.owner for t in sources}):
        rows, months[owner] = await plan_owner(s, owner, sources, through)
        entries.extend(rows)
    changes = [e for e in entries if not e.exists]
    manifest = [
        POLICY,
        through,
        [astuple(t) for t in sorted(sources)],
        [astuple(e) for e in entries],
        months,
    ]
    # JSON has no date keys. Preserve deterministic ordering, including the proposed monthly rank.
    manifest[-1] = [(u, sorted(ms.items())) for u, ms in sorted(months.items())]
    digest = hashlib.sha256(
        json.dumps(manifest, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    if apply_digest is not None:
        if digest != apply_digest:
            raise ValueError("Reviewed plan changed; nothing posted")
        for e in changes:
            await repo.upsert_accrual(
                s,
                user_id=e.target.owner,
                account_id=e.target.account,
                accrual_date=e.day,
                amount_usdt=e.source.gross,
                effective_rank=e.rank,
                fee_pct=e.fee,
                commission_usdt=e.source.gross - e.net,
                net_usdt=e.net,
                state="available",
                available_at=now,
                expires_at=now + timedelta(days=LIFETIME_DAYS),
                raw_records_count=e.source.count,
                adapter_notes=NOTE,
            )
        for owner in sorted({e.target.owner for e in changes}):
            for month, (rank, gross) in months[owner].items():
                await repo.write_or_bump_rank_history(
                    s, user_id=owner, period_month=month, achieved_rank=rank, gross_month_usdt=gross
                )
        for t in sorted({e.target for e in changes}):
            batch = [e for e in changes if e.target == t]
            s.add(
                AuditEvent(
                    actor="binance-recovery",
                    action="credit_reconciled_days",
                    entity_type="exchange_account",
                    entity_id=str(t.account),
                    amount_usdt=sum((e.net for e in batch), ZERO),
                    new_value={
                        "through": through.isoformat(),
                        "policy": POLICY,
                        "digest": digest,
                        "days": len(batch),
                    },
                )
            )
        await s.flush()
    return {
        "through": str(through),
        "digest": digest,
        "applied": apply_digest is not None,
        "accounts": [
            {
                "uid": t.uid,
                "days": len(sources[t]),
                "new_days": sum(e.target == t for e in changes),
                "gross": str(sum((v.gross for v in sources[t].values()), ZERO)),
                "new_net": str(sum((e.net for e in changes if e.target == t), ZERO)),
            }
            for t in sorted(sources)
        ],
    }


async def run(through: date, apply_digest: str | None) -> None:
    adapters = build_adapters(get_settings())
    engine = make_engine(DbSettings().database_url)
    sm = make_sessionmaker(engine)
    try:
        adapter = adapters.get("binance")
        if not isinstance(adapter, BinanceAdapter):
            raise ValueError("Live Binance adapter not enabled")
        async with sm() as s:
            await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            before = await targets(s, adapters)
        now = datetime.now(UTC)
        sources = {}
        for t in before:
            if through > now.astimezone(PAYOUT_TZ).date() - timedelta(days=1 + t.lag):
                raise ValueError("Requested cutoff includes unsettled days")
            sources[t] = await source_days(adapter, t, through)
        async with sm.begin() as s:
            if apply_digest is None:
                await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            else:
                await s.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                await s.execute(text("SET LOCAL lock_timeout = '5s'"))
                await s.execute(
                    text(
                        "LOCK TABLE exchange_accounts, users, invite_attributions, "
                        "rebate_accruals, rank_history IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
            if await targets(s, adapters) != before:
                raise ValueError("Bound account snapshot changed")
            outcome = await reconcile(s, sources, through, datetime.now(UTC), apply_digest)
        print(json.dumps(outcome, separators=(",", ":")))
    finally:
        for exchange in adapters.values():
            await exchange.aclose()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--through", required=True, type=date.fromisoformat)
    parser.add_argument("--apply-digest", help="exact digest from a reviewed read-only run")
    args = parser.parse_args()
    asyncio.run(run(args.through, args.apply_digest))
