"""Month-scale simulation of the whole rebate-bot lifecycle on an isolated DB.

Run: ``uv run python -m sim.harness``  (needs Docker for testcontainers).

Drives the REAL code paths — accrual worker, botui withdrawal service, admin
repository, payout executor — over ~6 simulated months for ~50 users, then prints
a report and asserts money/state invariants. On-chain sends use the in-memory
deterministic MockChainClient (two instances: BSC-USDT and Mantle-MNT, each with
its own nonce space); real testnet sends are a separate, later step.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Windows terminals default to a legacy codepage (cp1251) that can't encode the
# report's box-drawing/em-dash/emoji; force UTF-8 and never crash on output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, ValueError):
    pass

# --- Isolate config BEFORE any bot module reads settings ---------------------
# Env vars take precedence over any local .env, so the sim never inherits prod
# wiring. MANTLE_RPC_URL is a throwaway value: it only flips settings.mnt_enabled
# to True (botui's prepare_withdrawal reads it); no Mantle client is built from it
# here — the executor gets an in-memory mock instead.
os.environ.setdefault("BOT_TOKEN", "0:sim")
os.environ.setdefault("USER_TELEGRAM_IDS", "1")
os.environ.setdefault("DEVELOPER_TELEGRAM_ID", "1")
os.environ["MANTLE_RPC_URL"] = "http://sim.invalid"  # force mnt_enabled=True
os.environ.setdefault("ADMIN_TELEGRAM_IDS", "1")
# The accrual worker never counts rebate before REBATE_EPOCH; align it with the
# simulation's start date (EPOCH below) or the whole run accrues nothing.
os.environ["REBATE_EPOCH"] = "2026-01-01"

from cryptography.fernet import Fernet

_MASTER_KEY = Fernet.generate_key().decode("ascii")
os.environ["MASTER_KEY"] = _MASTER_KEY

from collections import defaultdict  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import UTC, date, datetime, time, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from random import Random  # noqa: E402

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

import bot.config as config  # noqa: E402
from bot.accrual.adapters import MappingAdapterProvider  # noqa: E402
from bot.accrual.worker import AccrualWorker  # noqa: E402
from bot.admin import repository as admin_repo  # noqa: E402
from bot.botui import repository as botui_repo  # noqa: E402
from bot.botui import service as botui  # noqa: E402
from bot.db.base import Base  # noqa: E402
from bot.db.engine import make_engine, make_sessionmaker  # noqa: E402
from bot.db.models.ledger import RebateAccrual  # noqa: E402
from bot.db.models.payouts import Payout  # noqa: E402
from bot.executor.chain import MockChainClient  # noqa: E402
from bot.executor.executor import PayoutExecutor  # noqa: E402
from bot.payout.expiry import PAYOUT_TZ  # noqa: E402
from bot.payout.rank_timing import month_start, next_month_start  # noqa: E402
from bot.security.secrets import SecretCipher  # noqa: E402
from sim import scenarios  # noqa: E402
from sim.fakes import FailureFlags, FakeAdapter, FakeBybitAdapter, Profile  # noqa: E402

# Quiet the worker/executor structlog+stdlib chatter so the report stands out.
logging.disable(logging.CRITICAL)

# --- Simulation parameters ---------------------------------------------------
EPOCH = date(2026, 1, 1)
DAYS = 100
NUM_USERS = 50
DEVELOPER_ID = 1
ADMIN_ID = 900_000  # a distinct admin acting on releases/overrides
AUTO_LIMIT = Decimal("200")
MIN_PAYOUT = Decimal("10")
CONFIRMATIONS = 15

EXCHANGES = ["gate", "kucoin", "mexc", "bitget", "bitmart", "okx", "bybit", "weex"]

_clock = {"now": datetime.combine(EPOCH, time(12, 0), tzinfo=UTC)}


def now() -> datetime:
    return _clock["now"]


def today() -> date:
    return now().astimezone(PAYOUT_TZ).date()


@dataclass
class SimUser:
    user_id: int
    username: str | None
    wallet: str
    # list of (exchange, uid)
    accounts: list[tuple[str, str]] = field(default_factory=list)
    tier_label: str = ""
    mnt: bool = False


def _addr_for(user_id: int) -> str:
    h = f"{user_id:040x}"
    return "0x" + h[-40:]


async def _accrual_total(sm: async_sessionmaker[AsyncSession], user_id: int) -> int:
    async with sm() as s:
        return int(
            await s.scalar(text(f"SELECT count(*) FROM rebate_accruals WHERE user_id={user_id}"))
            or 0
        )


async def _accrual_total_all(sm: async_sessionmaker[AsyncSession]) -> int:
    async with sm() as s:
        return int(await s.scalar(text("SELECT count(*) FROM rebate_accruals")) or 0)


def build_population() -> tuple[list[SimUser], dict[str, Profile]]:
    """~50 users spread across ranks 0-4; ~1/3 have a Bybit+MNT account, some have
    a second account on another exchange, ~1/5 carry a resolvable @username."""
    rng = Random(20260101)
    profiles: dict[str, Profile] = {}
    users: list[SimUser] = []

    # (label, usdt_lo, usdt_hi, count) — realistic daily USDT; monthly gross lands the rank.
    tiers = [
        ("rank0", Decimal("0.05"), Decimal("0.25"), 6),  # < $10/mo dust
        ("rank1", Decimal("0.40"), Decimal("2.50"), 15),  # $10-100/mo
        ("rank2", Decimal("4"), Decimal("25"), 15),  # $100-1000/mo
        ("rank3", Decimal("30"), Decimal("90"), 11),  # $1000-10000/mo
        ("rank4", Decimal("30"), Decimal("90"), 3),  # r2-3 normally; spike month -> r4
    ]
    SPIKE_MONTH = date(2026, 2, 1)

    uid_counter = 1
    user_id = 1000
    seed = 1
    for label, lo, hi, count in tiers:
        for _ in range(count):
            user_id += 1
            give_name = rng.random() < 0.2
            username = f"trader{user_id}" if give_name else None
            su = SimUser(
                user_id=user_id,
                username=username,
                wallet=_addr_for(user_id),
                tier_label=label,
            )
            # Primary account on a rotating exchange.
            primary_ex = EXCHANGES[uid_counter % len(EXCHANGES)]
            uid = f"{primary_ex}-{uid_counter:05d}"
            uid_counter += 1
            if label == "rank4":
                profiles[uid] = Profile(
                    seed=seed,
                    usdt_lo=lo,
                    usdt_hi=hi,
                    spike_month=SPIKE_MONTH,
                    spike_lo=Decimal("420"),
                    spike_hi=Decimal("480"),
                )
            else:
                profiles[uid] = Profile(seed=seed, usdt_lo=lo, usdt_hi=hi)
            seed += 1
            su.accounts.append((primary_ex, uid))

            # ~1/3 of non-dust users get a Bybit+MNT account.
            if label != "rank0" and rng.random() < 0.34:
                muid = f"bybit-{uid_counter:05d}"
                uid_counter += 1
                # MNT native per day sized so USDT-equiv clears the $10 pool over a month.
                profiles[muid] = Profile(
                    seed=seed,
                    usdt_lo=lo / 2,
                    usdt_hi=hi / 2,
                    mnt_lo=Decimal("8"),
                    mnt_hi=Decimal("55"),
                )
                seed += 1
                su.accounts.append(("bybit", muid))
                su.mnt = True

            users.append(su)
    return users, profiles


def build_provider(profiles: dict[str, Profile]) -> tuple[MappingAdapterProvider, FailureFlags]:
    flags = FailureFlags()
    adapters = {}
    for code in EXCHANGES:
        if code == "bybit":
            adapters[code] = FakeBybitAdapter(code, profiles, flags)
        else:
            adapters[code] = FakeAdapter(code, profiles, flags)
    return MappingAdapterProvider(adapters), flags


async def seed(
    sm: async_sessionmaker[AsyncSession], users: list[SimUser], cipher: SecretCipher
) -> None:
    async with sm() as s:
        # Admin + developer are real user rows in production (promoted via
        # ensure_role); several FKs point at them (payouts.approved_by,
        # system_flags.updated_by, payouts.overridden_by), so they must exist.
        for actor_id in (ADMIN_ID, DEVELOPER_ID):
            await botui_repo.upsert_user(s, actor_id)
            await admin_repo.set_user_status(s, actor_id, "active")
        await s.commit()
    async with sm() as s:
        for su in users:
            await botui_repo.upsert_user(s, su.user_id, username=su.username)
            await botui.set_wallet(s, cipher, su.user_id, su.wallet)
            for ex, uid in su.accounts:
                acc = await botui_repo.add_account(s, su.user_id, ex, uid, EPOCH)
                assert acc is not None, f"seed account collision {ex}:{uid}"
        await s.commit()


# --- Withdrawal simulation ---------------------------------------------------
async def try_withdraw(
    sm: async_sessionmaker[AsyncSession], su: SimUser, rng: Random
) -> str | None:
    """Attempt one withdrawal for a user through the real botui service.

    Prefers USDT; falls back to MNT when USDT isn't withdrawable but MNT is.
    Returns the asset withdrawn, or None. All eligibility (rank/cooldown/min/
    pending/pause/block) is enforced by the service, not here.
    """
    n = now()
    t = today()
    async with sm() as s:
        summ = await botui.balance_summary(s, su.user_id, n, t)
    asset: str | None = None
    if summ.withdrawable:
        asset = "USDT"
    elif summ.mnt_withdrawable:
        asset = "MNT"
    if asset is None:
        return None
    # Prefer MNT sometimes for MNT-capable users, to exercise both pools.
    if su.mnt and summ.mnt_withdrawable and summ.withdrawable and rng.random() < 0.4:
        asset = "MNT"

    async with sm() as s:
        prep = await botui.prepare_withdrawal(s, su.user_id, n, t, asset=asset)
        if isinstance(prep, botui.WithdrawBlocked):
            return None
        token = await botui.issue_withdraw_token(s, su.user_id, prep, n)
        await s.commit()
    async with sm() as s:
        res = await botui.confirm_withdrawal(s, su.user_id, token, n)
        await s.commit()
    if isinstance(res, Payout):
        return asset
    return None


async def count_states(sm: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    """(reserved payouts, in-flight payouts, pending_admin payouts)."""
    async with sm() as s:
        reserved = await s.scalar(
            select(func.count()).select_from(Payout).where(Payout.status == "reserved")
        )
        inflight = await s.scalar(
            select(func.count())
            .select_from(Payout)
            .where(Payout.status.in_(("submitting", "sent")))
        )
        pending = await s.scalar(
            select(func.count()).select_from(Payout).where(Payout.status == "pending_admin")
        )
    return int(reserved or 0), int(inflight or 0), int(pending or 0)


async def drain_executor(ex: PayoutExecutor, sm: async_sessionmaker[AsyncSession]) -> None:
    """Push every reserved/in-flight payout to a terminal state (mock chain confirms
    instantly). pending_admin rows are left for the admin-release step."""
    for _ in range(5000):
        await ex.process_next()
        await ex.poll_confirmations()
        reserved, inflight, _pending = await count_states(sm)
        if reserved == 0 and inflight == 0:
            return
    raise RuntimeError("executor drain did not converge")


async def admin_release_pending(sm: async_sessionmaker[AsyncSession]) -> int:
    """Admin approves every pending_admin payout (MNT: 'funded wallet, sending';
    large USDT: 'reviewed, ok'). Returns how many were released."""
    async with sm() as s:
        rows = await admin_repo.list_pending_admin(s)
        ids = [p.id for p in rows]
    released = 0
    for pid in ids:
        async with sm() as s:
            if await admin_repo.release_payout(s, pid, ADMIN_ID):
                released += 1
            await s.commit()
    return released


async def scenario_revert(
    sm: async_sessionmaker[AsyncSession],
    executor: PayoutExecutor,
    mock_bsc: MockChainClient,
    user_id: int,
) -> dict:
    """Force a small USDT withdrawal, broadcast it, then revert its on-chain tx so the
    payout ends 'failed' and its accruals return to available. Assumes a clean reserved
    slate (call at day start, before the day's withdrawal shuffle)."""
    n_now, t_today = now(), today()
    async with sm() as s:
        prep = await botui.prepare_withdrawal(s, user_id, n_now, t_today, asset="USDT")
    if not isinstance(prep, botui.WithdrawPreview):
        reason = getattr(prep, "reason", "no_preview") if prep is not None else "none"
        return {"applied": False, "reason": reason}
    async with sm() as s:
        token = await botui.issue_withdraw_token(s, user_id, prep, n_now)
        await s.commit()
    async with sm() as s:
        payout = await botui.confirm_withdrawal(s, user_id, token, n_now)
        await s.commit()
    if not isinstance(payout, Payout):
        return {"applied": False, "reason": "confirm_no_payout"}
    pid = payout.id
    await executor.process_next()  # clean slate -> claims this payout, broadcasts, marks sent
    async with sm() as s:
        p = await s.get(Payout, pid)
        nonce, sent_status = p.nonce, p.status
    if nonce is None or sent_status != "sent":
        return {"applied": False, "reason": f"not_sent:{sent_status}"}
    scenarios.revert_next_paid_tx(mock_bsc, nonce)  # force on-chain failure for that nonce
    await (
        executor.poll_confirmations()
    )  # -> finalize_failed('reverted'), accruals back to available
    async with sm() as s:
        p2 = await s.get(Payout, pid)
    return {"applied": True, "payout_id": pid, "nonce": nonce, "final_status": p2.status}


async def scenario_block_inflight(
    sm: async_sessionmaker[AsyncSession], executor: PayoutExecutor, user_id: int
) -> dict:
    """Create a reserved USDT payout, block the user, then let the executor claim it — the
    block gate fails the payout and returns the accruals. Then unblock. Clean-slate call."""
    n_now, t_today = now(), today()
    async with sm() as s:
        prep = await botui.prepare_withdrawal(s, user_id, n_now, t_today, asset="USDT")
    if not isinstance(prep, botui.WithdrawPreview):
        reason = getattr(prep, "reason", "no_preview") if prep is not None else "none"
        return {"applied": False, "reason": reason}
    async with sm() as s:
        token = await botui.issue_withdraw_token(s, user_id, prep, n_now)
        await s.commit()
    async with sm() as s:
        payout = await botui.confirm_withdrawal(s, user_id, token, n_now)
        await s.commit()
    if not isinstance(payout, Payout):
        return {"applied": False, "reason": "confirm_no_payout"}
    pid = payout.id
    async with sm() as s:
        await admin_repo.set_user_status(s, user_id, "blocked")
        await s.commit()
    await (
        executor.process_next()
    )  # claims reserved -> block gate -> finalize_failed('user_blocked')
    async with sm() as s:
        p = await s.get(Payout, pid)
        final_status = p.status
        await admin_repo.set_user_status(s, user_id, "active")
        await s.commit()
    return {"applied": True, "payout_id": pid, "final_status": final_status}


# --- Scenario events (admin features) ----------------------------------------
async def event_rate_override(
    sm: async_sessionmaker[AsyncSession], su: SimUser, events: dict
) -> None:
    """Admin sets a personal rate BY NICKNAME and recomputes the current month."""
    n_now = today()
    m_start = month_start(n_now)
    m_before = next_month_start(m_start)
    new_pct = Decimal("0.02")
    async with sm() as s:
        uid = await botui_repo.find_user_id_by_username(s, f"@{su.username}")
        assert uid == su.user_id, f"nick resolve failed: {su.username} -> {uid}"

        def _avg_fee_stmt() -> object:
            return select(func.avg(RebateAccrual.fee_pct), func.sum(RebateAccrual.net_usdt)).where(
                RebateAccrual.user_id == uid,
                RebateAccrual.accrual_date >= m_start,
                RebateAccrual.accrual_date < m_before,
                RebateAccrual.state.in_(("available", "accruing")),
                RebateAccrual.effective_rank >= 1,
            )

        before = (await s.execute(_avg_fee_stmt())).first()
        await admin_repo.set_commission_override(s, su.user_id, new_pct, n_now)
        updated = await admin_repo.recompute_current_month_unpaid(
            s, su.user_id, new_pct, m_start, m_before
        )
        after = (await s.execute(_avg_fee_stmt())).first()
        await s.commit()
    events["override"] = {
        "user": su.user_id,
        "nick": su.username,
        "rows_updated": updated,
        "fee_before": None if before[0] is None else Decimal(before[0]),
        "fee_after": None if after[0] is None else Decimal(after[0]),
        "net_before": Decimal(before[1] or 0),
        "net_after": Decimal(after[1] or 0),
        "new_pct": new_pct,
    }


async def event_pause_check(
    sm: async_sessionmaker[AsyncSession], su: SimUser, events: dict
) -> None:
    """Pause payouts, confirm a withdrawal is refused, then resume."""
    async with sm() as s:
        await admin_repo.set_flag(s, admin_repo.PAYOUTS_PAUSED_KEY, {"paused": True}, ADMIN_ID)
        await s.commit()
    async with sm() as s:
        prep = await botui.prepare_withdrawal(s, su.user_id, now(), today())
    blocked = isinstance(prep, botui.WithdrawBlocked) and prep.reason == "paused"
    async with sm() as s:
        await admin_repo.set_flag(s, admin_repo.PAYOUTS_PAUSED_KEY, {"paused": False}, ADMIN_ID)
        await s.commit()
    events["pause"] = {"user": su.user_id, "blocked_while_paused": blocked}


async def event_block_check(
    sm: async_sessionmaker[AsyncSession], su: SimUser, events: dict
) -> None:
    """Block a user, confirm withdrawal refused, then unblock."""
    async with sm() as s:
        await admin_repo.set_user_status(s, su.user_id, "blocked")
        await s.commit()
    async with sm() as s:
        prep = await botui.prepare_withdrawal(s, su.user_id, now(), today())
    blocked = isinstance(prep, botui.WithdrawBlocked) and prep.reason == "blocked"
    async with sm() as s:
        await admin_repo.set_user_status(s, su.user_id, "active")
        await s.commit()
    events["block"] = {"user": su.user_id, "blocked_while_blocked": blocked}


# --- Report + invariants -----------------------------------------------------
async def _scalar(sm: async_sessionmaker[AsyncSession], stmt: object) -> object:
    async with sm() as s:
        return await s.scalar(stmt)


async def build_report(
    sm: async_sessionmaker[AsyncSession], users: list[SimUser], events: dict
) -> None:
    m_start = month_start(today())
    m_before = next_month_start(m_start)
    async with sm() as s:
        overview = await admin_repo.admin_overview(s, month_start=m_start, month_before=m_before)

    # accrual state x asset
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT state, asset, count(*), "
                    "coalesce(sum(amount_usdt),0), coalesce(sum(net_usdt),0), "
                    "coalesce(sum(net_native),0) "
                    "FROM rebate_accruals GROUP BY state, asset ORDER BY state, asset"
                )
            )
        ).all()
    # payout status x asset
    async with sm() as s:
        prows = (
            await s.execute(
                text(
                    "SELECT status, asset, count(*), coalesce(sum(net_usdt),0), "
                    "coalesce(sum(net_native),0) "
                    "FROM payouts GROUP BY status, asset ORDER BY status, asset"
                )
            )
        ).all()
    # rank history distribution (latest closed month per user)
    async with sm() as s:
        rank_rows = (
            await s.execute(
                text(
                    "SELECT achieved_rank, count(*) FROM ("
                    "  SELECT DISTINCT ON (user_id) user_id, achieved_rank "
                    "  FROM rank_history ORDER BY user_id, period_month DESC"
                    ") t GROUP BY achieved_rank ORDER BY achieved_rank"
                )
            )
        ).all()

    print("\n" + "=" * 72)
    print(
        f"SIMULATION REPORT — {DAYS} days, {len(users)} users, "
        f"{EPOCH} .. {EPOCH + timedelta(days=DAYS)}"
    )
    print("=" * 72)

    print("\n-- Business-wide earnings (admin 📊) --")
    print(f"  earned  total={overview['earned_total']:.2f}  month={overview['earned_month']:.2f}")
    print(f"  rebate  total={overview['rebate_total']:.2f}  month={overview['rebate_month']:.2f}")
    print(f"  profit  total={overview['profit_total']:.2f}  month={overview['profit_month']:.2f}")
    print(
        f"  users={overview['users_total']} blocked={overview['users_blocked']} "
        f"active_accounts={overview['active_accounts']}"
    )
    print(
        f"  available_net={overview['available_net']:.2f} reserved={overview['reserved']} "
        f"pending_admin={overview['pending_admin']} in_flight={overview['in_flight']}"
    )
    print(
        f"  paid_count={overview['paid_count']} paid_sum={overview['paid_sum']:.2f} "
        f"paused={overview['paused']}"
    )

    print("\n-- Accruals by (state, asset): count / Σamount_usdt / Σnet_usdt / Σnet_native --")
    for st, asset, cnt, amt, net, natv in rows:
        print(
            f"  {st:<10} {asset:<5} n={cnt:<6} amt={Decimal(amt):>14.2f} "
            f"net={Decimal(net):>14.2f} native={Decimal(natv):>14.4f}"
        )

    print("\n-- Payouts by (status, asset): count / Σnet_usdt / Σnet_native --")
    for st, asset, cnt, net, natv in prows:
        print(
            f"  {st:<13} {asset:<5} n={cnt:<5} net_usdt={Decimal(net):>13.2f} "
            f"net_native={Decimal(natv):>13.4f}"
        )

    print("\n-- Latest achieved rank distribution --")
    for rank, cnt in rank_rows:
        print(f"  rank {rank}: {cnt} users")

    async with sm() as s:
        max_rank_rows = (
            await s.execute(
                text(
                    "SELECT achieved_rank, count(*) FROM ("
                    "  SELECT user_id, max(achieved_rank) achieved_rank "
                    "  FROM rank_history GROUP BY user_id"
                    ") t GROUP BY achieved_rank ORDER BY achieved_rank"
                )
            )
        ).all()
    print("\n-- Max rank ever achieved (any closed month) --")
    for rank, cnt in max_rank_rows:
        print(f"  rank {rank}: {cnt} users")

    if "override" in events:
        e = events["override"]
        fb = "n/a" if e["fee_before"] is None else f"{e['fee_before']:.4f}"
        fa = "n/a" if e["fee_after"] is None else f"{e['fee_after']:.4f}"
        print("\n-- Admin rate-by-nickname override --")
        print(
            f"  @{e['nick']} (user {e['user']}) -> pct={e['new_pct']}  rows_recomputed={e['rows_updated']}"
        )
        print(
            f"  current-month avg fee_pct: {fb} -> {fa}   "
            f"net_usdt: {e['net_before']:.2f} -> {e['net_after']:.2f}"
        )
    if "pause" in events:
        print("\n-- Admin pause / block gates --")
        print(
            f"  pause: withdrawal refused while paused = {events['pause']['blocked_while_paused']}"
        )
    if "block" in events:
        print(
            f"  block: withdrawal refused while blocked = {events['block']['blocked_while_blocked']}"
        )

    print("\n-- New scenarios --")
    for key in (
        "new_user",
        "collision",
        "reassign",
        "block_uid",
        "reduce",
        "downtime",
        "mnt_rate_failure",
        "adapter_failure",
        "revert",
        "block_inflight",
    ):
        if key in events:
            print(f"  {key}: {events[key]}")


async def check_invariants(sm: async_sessionmaker[AsyncSession], events: dict) -> bool:
    checks: list[tuple[str, bool, str]] = []

    async def cnt(sql: str) -> int:
        async with sm() as s:
            return int((await s.scalar(text(sql))) or 0)

    # 1. Per-accrual money identity: commission == amount - net (both USDT and native).
    bad = await cnt(
        "SELECT count(*) FROM rebate_accruals "
        "WHERE abs(amount_usdt - net_usdt - commission_usdt) > 1e-9"
    )
    checks.append(
        ("accrual money identity (amount = net + commission)", bad == 0, f"{bad} rows off")
    )

    # 2. No negative money anywhere.
    bad = await cnt(
        "SELECT count(*) FROM rebate_accruals "
        "WHERE amount_usdt < 0 OR net_usdt < 0 OR commission_usdt < 0 OR net_native < 0"
    )
    checks.append(("no negative accrual amounts", bad == 0, f"{bad} rows"))

    # 3. Business identity: earned == rebate + profit.
    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT coalesce(sum(amount_usdt),0), coalesce(sum(net_usdt),0), "
                    "coalesce(sum(commission_usdt),0) FROM rebate_accruals"
                )
            )
        ).first()
    earned, rebate, profit = Decimal(row[0]), Decimal(row[1]), Decimal(row[2])
    ok = abs(earned - rebate - profit) < Decimal("0.000001")
    checks.append(("earned = rebate + profit", ok, f"{earned:.2f} vs {rebate + profit:.2f}"))

    # 4. Every non-terminal/sent payout has a txhash+nonce once it left 'submitting'.
    bad = await cnt(
        "SELECT count(*) FROM payouts WHERE status IN ('sent','paid') "
        "AND (txhash IS NULL OR nonce IS NULL)"
    )
    checks.append(("sent/paid payouts have txhash+nonce", bad == 0, f"{bad} rows"))

    # 5. At most one ACTIVE payout per user (reserved/pending_admin/submitting/sent).
    bad = await cnt(
        "SELECT count(*) FROM (SELECT user_id FROM payouts "
        "WHERE status IN ('reserved','pending_admin','submitting','sent') "
        "GROUP BY user_id HAVING count(*) > 1) t"
    )
    checks.append(("<=1 active payout per user", bad == 0, f"{bad} users with >1"))

    # 6. Every MNT payout that got sent/paid was admin-approved (approved_at set).
    bad = await cnt(
        "SELECT count(*) FROM payouts WHERE asset='MNT' AND status IN ('sent','paid') "
        "AND approved_at IS NULL"
    )
    checks.append(("MNT sent/paid were admin-approved", bad == 0, f"{bad} rows"))

    # 7. accrual state 'paid' <=> its payout is 'paid'.
    bad = await cnt(
        "SELECT count(*) FROM rebate_accruals a JOIN payouts p ON a.payout_id = p.id "
        "WHERE a.state='paid' AND p.status <> 'paid'"
    )
    checks.append(("paid accruals belong to paid payouts", bad == 0, f"{bad} rows"))

    # 8. No 'available' accrual is past its expiry (expire_aged ran on the last cycle).
    bad = await cnt(
        f"SELECT count(*) FROM rebate_accruals "
        f"WHERE state='available' AND expires_at <= '{now().isoformat()}'"
    )
    checks.append(("no available accrual past expiry", bad == 0, f"{bad} rows"))

    # 9. reserved accruals point to a still-active payout; paid payouts sum <= accrued net.
    bad = await cnt(
        "SELECT count(*) FROM rebate_accruals a LEFT JOIN payouts p ON a.payout_id = p.id "
        "WHERE a.state='reserved' AND (p.id IS NULL OR p.status NOT IN "
        "('reserved','pending_admin','submitting','sent'))"
    )
    checks.append(("reserved accruals linked to active payout", bad == 0, f"{bad} rows"))

    # 10. Two successful USDT payouts for one user never land < 1 day apart (min cooldown).
    bad = await cnt(
        "SELECT count(*) FROM ("
        "  SELECT user_id, confirmed_at, "
        "  lag(confirmed_at) OVER (PARTITION BY user_id ORDER BY confirmed_at) prev "
        "  FROM payouts WHERE status='paid' AND confirmed_at IS NOT NULL"
        ") t WHERE prev IS NOT NULL AND confirmed_at - prev < interval '1 day'"
    )
    checks.append(("paid payouts respect >=1d min cooldown", bad == 0, f"{bad} violations"))

    # 11b. Realism guard: no whale reintroduction — per-user cumulative rebate and total sane.
    async with sm() as s:
        max_user = Decimal(
            await s.scalar(
                text(
                    "SELECT coalesce(max(u),0) FROM ("
                    "  SELECT sum(net_usdt) u FROM rebate_accruals GROUP BY user_id"
                    ") t"
                )
            )
            or 0
        )
        total_net = Decimal(
            await s.scalar(text("SELECT coalesce(sum(net_usdt),0) FROM rebate_accruals")) or 0
        )
    checks.append(
        (
            "realism: max per-user rebate <= 20k",
            max_user <= Decimal("20000"),
            f"max_user={max_user:.2f}",
        )
    )
    checks.append(
        ("realism: total rebate <= 300k", total_net <= Decimal("300000"), f"total={total_net:.2f}")
    )

    # 11. Scenario gates fired.
    if "override" in events:
        e = events["override"]
        ok = e["rows_updated"] > 0 and (
            e["fee_after"] is None or e["fee_after"] <= e["new_pct"] + Decimal("1e-9")
        )
        checks.append(
            (
                "rate override recomputed current month",
                ok,
                f"updated={e['rows_updated']} fee_after={e['fee_after']}",
            )
        )
    if events.get("pause"):
        checks.append(("pause blocks withdrawal", events["pause"]["blocked_while_paused"], ""))
    if events.get("block"):
        checks.append(("block blocks withdrawal", events["block"]["blocked_while_blocked"], ""))

    # --- new-scenario invariants ---
    # A failed/cancelled payout leaves NO accrual stuck in 'reserved'.
    bad = await cnt(
        "SELECT count(*) FROM rebate_accruals a JOIN payouts p ON a.payout_id = p.id "
        "WHERE a.state='reserved' AND p.status IN ('failed','cancelled')"
    )
    checks.append(("failed/cancelled payouts left no reserved accruals", bad == 0, f"{bad} rows"))

    # Reassign: after reassignment the (exchange, uid) account is owned by the new user.
    if events.get("reassign", {}).get("applied"):
        e = events["reassign"]
        ok = e["new_holder"] == e["to"]
        checks.append(
            ("reassign UID moved ownership", ok, f"holder={e['new_holder']} want={e['to']}")
        )

    # Block UID: the account is now 'blocked'.
    if events.get("block_uid", {}).get("applied"):
        ok = events["block_uid"]["status_now"] == "blocked"
        checks.append(("block UID set status=blocked", ok, str(events["block_uid"]["status_now"])))

    # UID collision: the second claimant was rejected (first-come wins).
    if "collision" in events:
        ok = events["collision"]["rejected"] is True
        checks.append(("UID collision rejected (first-come)", ok, str(events["collision"])))

    # Reduce-commission: net went UP (or equal) and never exceeds gross.
    if events.get("reduce", {}).get("applied"):
        e = events["reduce"]
        ok = e["net_after"] >= e["net_before"]
        checks.append(
            ("reduce-commission raised net", ok, f"{e['net_before']:.4f} -> {e['net_after']:.4f}")
        )

    # MNT-rate outage: the Bybit-holding probe froze NOTHING that cycle (whole user skipped),
    # other users still processed, and the probe backfilled the next cycle.
    if "mnt_rate_failure" in events:
        e = events["mnt_rate_failure"]
        ok = (
            e["bybit_grew_55"] == 0
            and e["others_grew_55"] >= 1
            and e["bybit_recovered_56"] > e["bybit_total_before"]
        )
        checks.append(("MNT-rate outage: bybit skipped+backfilled, peers ok", ok, str(e)))

    # Adapter outage: the failing user froze nothing that cycle; other users still did.
    if "adapter_failure" in events:
        e = events["adapter_failure"]
        ok = e["fail_grew"] == 0 and e["others_grew"] >= 1
        checks.append(("adapter outage: failing user skipped, peers ok", ok, str(e)))

    # Reverted tx: the broadcast payout ended 'failed' (accruals returned — see the
    # 'failed/cancelled left no reserved accruals' invariant, now non-vacuous).
    if events.get("revert", {}).get("applied"):
        ok = events["revert"]["final_status"] == "failed"
        checks.append(("reverted tx -> payout failed", ok, str(events["revert"])))

    # Blocked-in-flight: a reserved payout for a just-blocked user is failed, funds returned.
    if events.get("block_inflight", {}).get("applied"):
        ok = events["block_inflight"]["final_status"] == "failed"
        checks.append(("blocked in-flight -> payout failed", ok, str(events["block_inflight"])))

    # Downtime recorded a service_availability window.
    if "downtime" in events:
        n = await cnt("SELECT count(*) FROM service_availability")
        checks.append(("downtime recorded", n >= 1, f"{n} windows"))

    # Every designed scenario MUST have fired — otherwise a silently-skipped scenario would
    # let the guarded checks above vanish while the run still prints ALL PASSED.
    required = {
        "new_user": lambda e: e.get("added") is True,
        "collision": lambda e: e.get("rejected") is True,
        "reassign": lambda e: e.get("applied") is True,
        "block_uid": lambda e: e.get("applied") is True,
        "reduce": lambda e: e.get("applied") is True,
        "override": lambda e: e.get("rows_updated", 0) > 0,
        "pause": lambda e: e.get("blocked_while_paused") is True,
        "block": lambda e: e.get("blocked_while_blocked") is True,
        "downtime": lambda e: bool(e.get("skipped_days")),
        "mnt_rate_failure": lambda e: "bybit_grew_55" in e,
        "adapter_failure": lambda e: "fail_grew" in e,
        "revert": lambda e: e.get("applied") is True,
        "block_inflight": lambda e: e.get("applied") is True,
    }
    for key, pred in required.items():
        fired = key in events and pred(events[key])
        checks.append((f"scenario fired: {key}", fired, str(events.get(key))))

    print("\n-- Invariants --")
    all_ok = True
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        suffix = "" if ok else f"   <-- {detail}"
        print(f"  [{mark}] {name}{suffix}")
    print("\n" + ("ALL INVARIANTS PASSED ✅" if all_ok else "INVARIANT FAILURES ❌"))
    return all_ok


async def main() -> None:
    users, profiles = build_population()
    provider, flags = build_provider(profiles)
    cipher = SecretCipher(_MASTER_KEY)

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        engine = make_engine(pg.get_connection_url())
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)

        # settings singleton now sees the isolated env (mnt_enabled=True)
        config._settings = None
        settings = config.get_settings()
        assert settings.mnt_enabled, "MNT must be enabled for the sim"

        await seed(sm, users, cipher)

        worker = AccrualWorker(sm, provider, now=now, developer_id=DEVELOPER_ID, settings=settings)
        bsc = MockChainClient(nonce=0)
        mantle = MockChainClient(nonce=0)
        executor = PayoutExecutor(
            sm,
            bsc,
            cipher,
            now=now,
            wallet_address="0xsimwallet",
            auto_limit=AUTO_LIMIT,
            min_payout=MIN_PAYOUT,
            confirmations=CONFIRMATIONS,
            developer_id=DEVELOPER_ID,
            mantle_chain=mantle,
        )

        events: dict = {}
        rng = Random(777)
        override_user = next(
            (u for u in users if u.username and u.tier_label in ("rank2", "rank3")), None
        )
        pause_user = next(u for u in reversed(users) if u.tier_label == "rank4")
        block_user = next(u for u in users if u.tier_label == "rank4")

        # actors for new scenarios (deterministic picks)
        rank3_users = [u for u in users if u.tier_label == "rank3"]
        reassign_from = rank3_users[0]
        reassign_to = rank3_users[1]
        reassign_ex, reassign_uid = reassign_from.accounts[0]
        block_uid_user = rank3_users[2]
        block_uid_ex, block_uid_uid = block_uid_user.accounts[0]
        adapter_fail_user = next(u for u in users if u.tier_label == "rank2")
        adapter_fail_ex, adapter_fail_uid = adapter_fail_user.accounts[0]

        # reduce-commission needs a genuine pending_admin USDT payout on day 100 that
        # actually carries a nonzero commission (a rank4 pick would not: rank4's floor
        # protection plus fee_pct(4)==0 means nothing to reduce). A payout only reaches
        # pending_admin for the instant an executor tick sees it over AUTO_LIMIT (day
        # cycles fully drain pending_admin via admin auto-release every day, so nothing
        # persists across days) — so this rank3 "saver" (fee_pct=3%, not otherwise used
        # by reassign/block_uid above) is excluded from the daily random withdrawal loop
        # below to bank >30 days of accruals (comfortably over AUTO_LIMIT), then forced
        # to withdraw+process on day 100 itself, right before the reduce-commission call.
        reduce_target = rank3_users[3]

        rank1_users = [u for u in users if u.tier_label == "rank1"]
        revert_user = rank1_users[0]  # small banked balance -> reserved+sent (revertable)
        block_inflight_user = rank1_users[1]
        bybit_probe = next(u for u in users if u.mnt)

        wd_counts: dict[str, int] = defaultdict(int)
        for day in range(1, DAYS + 1):
            _clock["now"] = datetime.combine(EPOCH + timedelta(days=day), time(12, 0), tzinfo=UTC)

            # --- downtime: skip cycles on days 40-42 so a >2-day gap is recorded (grace) ---
            if day in (40, 41, 42):
                if day == 42:
                    events["downtime"] = {"skipped_days": [40, 41, 42]}
                continue

            # --- inject failure flags for exactly one cycle + capture observable effect ---
            flags.fail_uid = adapter_fail_uid if day == 50 else None
            flags.fail_mnt_rate = day == 55
            if day == 50:
                _pre_fail = await _accrual_total(sm, adapter_fail_user.user_id)
                _pre_total50 = await _accrual_total_all(sm)
            if day == 55:
                _pre_bybit = await _accrual_total(sm, bybit_probe.user_id)
                _pre_total55 = await _accrual_total_all(sm)

            await worker.run_cycle()

            if day == 50:
                events["adapter_failure"] = {
                    "user": adapter_fail_user.user_id,
                    "fail_grew": (await _accrual_total(sm, adapter_fail_user.user_id)) - _pre_fail,
                    "others_grew": (await _accrual_total_all(sm)) - _pre_total50,
                }
            if day == 51:
                events["adapter_failure"]["fail_recovered"] = await _accrual_total(
                    sm, adapter_fail_user.user_id
                )
            if day == 55:
                events["mnt_rate_failure"] = {
                    "user": bybit_probe.user_id,
                    "bybit_grew_55": (await _accrual_total(sm, bybit_probe.user_id)) - _pre_bybit,
                    "others_grew_55": (await _accrual_total_all(sm)) - _pre_total55,
                    "bybit_total_before": _pre_bybit,
                }
            if day == 56:
                events["mnt_rate_failure"]["bybit_recovered_56"] = await _accrual_total(
                    sm, bybit_probe.user_id
                )

            # --- scheduled scenario events ---
            if day == 20:
                events["new_user"] = await scenarios.event_new_user_mid_sim(
                    sm,
                    cipher,
                    5001,
                    "latejoiner",
                    _addr_for(5001),
                    "gate",
                    "gate-90001",
                    EPOCH + timedelta(days=day - 1),
                )
            if day == 25:
                events["collision"] = await scenarios.event_uid_collision(
                    sm,
                    adapter_fail_ex,
                    adapter_fail_uid,
                    5001,
                )
            if day == 60:
                events["reassign"] = await scenarios.event_reassign_uid(
                    sm,
                    reassign_ex,
                    reassign_uid,
                    reassign_from.user_id,
                    reassign_to.user_id,
                )
            if day == 65:
                events["block_uid"] = await scenarios.event_block_uid(
                    sm,
                    block_uid_ex,
                    block_uid_uid,
                )
            if day == 95 and override_user is not None:
                await event_rate_override(sm, override_user, events)
            if day == 85:
                await event_pause_check(sm, pause_user, events)
            if day == 90:
                await event_block_check(sm, block_user, events)
            if day == 70:
                events["revert"] = await scenario_revert(sm, executor, bsc, revert_user.user_id)
            if day == 75:
                events["block_inflight"] = await scenario_block_inflight(
                    sm, executor, block_inflight_user.user_id
                )

            # --- reduce-commission setup: force the saver's banked USDT balance into
            # a real payout and push it past the auto-limit gate to pending_admin,
            # BEFORE the reduce-commission scenario call looks for one. ---
            if day == 100:
                n_now, t_today = now(), today()
                async with sm() as s:
                    prep = await botui.prepare_withdrawal(
                        s, reduce_target.user_id, n_now, t_today, asset="USDT"
                    )
                if isinstance(prep, botui.WithdrawPreview):
                    async with sm() as s:
                        token = await botui.issue_withdraw_token(
                            s, reduce_target.user_id, prep, n_now
                        )
                        await s.commit()
                    async with sm() as s:
                        await botui.confirm_withdrawal(s, reduce_target.user_id, token, n_now)
                        await s.commit()
                    # single-flight claim: with a clean reserved/in-flight slate at day
                    # start, this claims exactly the payout just reserved above.
                    await executor.process_next()

            # --- withdrawals (the reduce-commission saver sits out, see above) ---
            _excluded = {reduce_target.user_id, revert_user.user_id, block_inflight_user.user_id}
            order = [u for u in users if u.user_id not in _excluded]
            rng.shuffle(order)
            for su in order:
                if rng.random() < 0.5:
                    asset = await try_withdraw(sm, su, rng)
                    if asset:
                        wd_counts[asset] += 1

            # --- reduce-commission: on day 100, if any pending_admin USDT exists ---
            if day == 100:
                events["reduce"] = await scenarios.event_reduce_commission(sm, ADMIN_ID)

            await drain_executor(executor, sm)
            if await admin_release_pending(sm):
                await drain_executor(executor, sm)

        print(f"\nwithdrawals confirmed: USDT={wd_counts['USDT']} MNT={wd_counts['MNT']}")
        await build_report(sm, users, events)
        ok = await check_invariants(sm, events)
        await engine.dispose()
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
