# bot/accrual/worker.py
"""The accrual worker: one idempotent cycle turns closed exchange days into frozen
rebate_accruals rows, closes past months (records achieved rank) and expires aged
rows. Ranks are per-user (summed across UIDs); rows are per account-day. A sub-$10
month is not destroyed — it just stays non-withdrawable (rank-0 gate) and expires
on the normal 30-day schedule.

See docs/superpowers/plans/2026-07-13-accrual-worker.md and the option-C decision
in docs/superpowers/specs/2026-07-12-accrual-worker-design.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.accrual import fetches
from bot.accrual import repository as repo
from bot.accrual.adapters import AdapterProvider
from bot.config import Settings, get_settings
from bot.exchanges.base import CommissionResult, ExchangeAdapter, ExchangeApiError
from bot.notifications import repository as notif_repo
from bot.partners import rank_control
from bot.payout.commission import effective_fee
from bot.payout.expiry import PAYOUT_TZ, available_at, expires_at
from bot.payout.rank_timing import (
    day_effective_rank,
    iter_days,
    month_start,
    next_month_start,
    prev_month_start,
)
from bot.payout.ranks import rank_for_gross

log = structlog.get_logger(__name__)

# A gap between heartbeats longer than this means at least one daily cycle was
# missed — the service was down — so the gap is recorded as a downtime window.
_DOWNTIME_THRESHOLD = timedelta(days=2)

# Whole-downline snapshot spike guard (see _refresh_downline_snapshot).
# ONLY OKX's invitee/list returns unstable PRELIMINARY totals that the exchange
# silently revises by 10-20x hours later (observed: OKX flapping 202 -> 4703 -> 2099
# for the same window). Every OTHER exchange returns stable figures and is ALWAYS
# reported raw — no smoothing, no history, no flag. For OKX alone we keep a rolling
# window of its last N readings (carried in the snapshot's ``history``) and, only for
# a LARGE reading that also jumped sharply, report the window MEDIAN instead of the raw
# value — outvoting a lone spike. Self-healing: a spike that persists becomes the median.
_DOWNLINE_SPIKE_GUARD_EXCHANGES = frozenset({"okx"})
_DOWNLINE_HISTORY = 3
# A guarded exchange's reading is smoothed (median reported, admin flagged) ONLY when
# BOTH: it clears the absolute FLOOR, and it diverges from the recent median by
# >= FACTOR. FLOOR keeps OKX's own ordinary total (~$200) reported raw; only the
# thousands-scale garbage that actually distorts the grand total engages the median.
# FACTOR sits below the observed garbage (10-23x) yet above any real per-cycle move.
_DOWNLINE_GUARD_FACTOR = Decimal(4)
_DOWNLINE_GUARD_FLOOR = Decimal(300)


def _parse_history(raw: object) -> list[Decimal]:
    """Parse a stored per-exchange reading history to Decimals, dropping any corrupt
    entries (a bad stored value must never crash the refresh)."""
    if not isinstance(raw, list):
        return []
    out: list[Decimal] = []
    for item in raw:
        try:
            out.append(Decimal(str(item)))
        except (InvalidOperation, ValueError):
            continue
    return out


def _downline_median(values: list[Decimal]) -> Decimal:
    """Median of ``values``; on an even count take the LOWER middle so a single high
    spike cannot pull the reported figure up. Empty -> 0."""
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _is_downline_outlier(reported: Decimal, current: Decimal) -> bool:
    """True when ``current`` diverges from the ``reported`` median enough to flag it
    as a smoothed-away spike. Both directions; sub-floor values are treated as noise
    and never flagged so legitimate early-month growth is quiet."""
    hi = max(reported, current)
    lo = min(reported, current)
    if hi < _DOWNLINE_GUARD_FLOOR:
        return False
    if lo <= 0:
        return True
    return hi / lo >= _DOWNLINE_GUARD_FACTOR


@dataclass(frozen=True, slots=True)
class _AccountRef:
    """Plain snapshot of the ExchangeAccount fields the worker needs.

    Extracted from the ORM object while its (short-lived, read-only) session is
    still open, so later per-user sessions never touch a detached instance.
    """

    id: int
    exchange: str
    uid: str
    accrual_since: date


@dataclass(frozen=True, slots=True)
class _FetchedDay:
    query_from: datetime
    query_to: datetime
    result: CommissionResult


def _day_bounds_payout_tz(d: date) -> tuple[datetime, datetime]:
    """[00:00, 23:59:59.999999] of calendar day ``d`` in PAYOUT_TZ, tz-aware.

    Deliberately kept in PAYOUT_TZ rather than converted to UTC: every adapter in
    this repo calls ``date_from.date()`` / ``date_to.date()`` to build the calendar
    date it sends the exchange (see e.g. bot/exchanges/bybit.py, gate.py, mexc.py),
    matching the exchange's UTC+8 affiliate day. Converting to UTC first would
    shift ``.date()`` back a day for any time before 08:00 PAYOUT_TZ and silently
    desync the accrual day from the day actually queried.
    """
    start = datetime.combine(d, time.min, tzinfo=PAYOUT_TZ)
    end = datetime.combine(d, time.max, tzinfo=PAYOUT_TZ)
    return start, end


class AccrualWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider: AdapterProvider,
        *,
        now: Callable[[], datetime],
        developer_id: int | None = None,
        downtime_threshold: timedelta = _DOWNTIME_THRESHOLD,
        settings: Settings | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._provider = provider
        self._now = now
        settings = settings or get_settings()
        self._epoch = settings.rebate_epoch
        self._mnt_enabled = settings.mnt_enabled
        # Exchanges with configured credentials — iterated to refresh the
        # whole-downline rebate snapshot at the end of each cycle.
        self._exchange_codes = tuple(sorted(settings.available_exchanges))
        self._developer_id = developer_id
        self._downtime_threshold = downtime_threshold
        # MNT->USDT rate cache: fetched at most once per run_cycle (see
        # _get_mnt_rate), reset at the top of each cycle.
        self._mnt_rate: Decimal | None = None
        self._mnt_rate_failed = False

    def _today(self) -> date:
        return self._now().astimezone(PAYOUT_TZ).date()

    async def _get_mnt_rate(self, adapter: ExchangeAdapter) -> Decimal | None:
        """MNT->USDT rate for this cycle, fetched at most once and cached.

        Returns ``None`` if MNT support isn't wired for ``adapter`` (no
        ``mnt_usdt_rate`` method — the base adapter type doesn't declare one) or if
        an earlier fetch this cycle already failed. On a fresh failure: log + dev
        alert, cache the failure, and return ``None`` — MNT accrual is skipped for
        the rest of this cycle while USDT accrual proceeds normally.
        """
        if self._mnt_rate is not None:
            return self._mnt_rate
        if self._mnt_rate_failed:
            return None
        fetch = getattr(adapter, "mnt_usdt_rate", None)
        if fetch is None:
            return None
        try:
            rate: Decimal = await fetch()
        except Exception as exc:
            self._mnt_rate_failed = True
            log.warning("mnt_rate_fetch_failed", error=str(exc))
            await self._dev_alert("MNT rate fetch failed", f"{type(exc).__name__}: {exc}")
            return None
        self._mnt_rate = rate
        return rate

    async def _dev_alert(self, title: str, detail: str) -> None:
        """Enqueue a developer alert via the outbox (best-effort, own session).

        Best-effort: the bot process (not this one) actually sends it, and a failure
        to even enqueue must never abort or roll back accrual work — so this opens
        its own session and swallows any error after logging it. No-op if no
        developer chat is configured."""
        if self._developer_id is None:
            return
        try:
            async with self._sm() as s:
                await notif_repo.enqueue(
                    s,
                    recipient_chat_id=self._developer_id,
                    kind=notif_repo.DEV_ALERT,
                    payload={"title": title, "detail": detail[:1000]},
                )
                await s.commit()
        except Exception:
            log.exception("accrual_dev_alert_enqueue_failed")

    async def report_cycle_failure(self, exc: BaseException) -> None:
        """Alert the developer that a whole accrual cycle raised (called by the
        scheduler after it logs+swallows the error to keep the loop alive)."""
        await self._dev_alert("Accrual cycle failed", f"{type(exc).__name__}: {exc}")

    async def _check_downtime(self, now: datetime) -> None:
        """Detect a missed-cycle gap via the heartbeat and record it as downtime,
        then re-stamp the heartbeat. Runs in its own committed session."""
        async with self._sm() as s:
            last = await repo.get_heartbeat(s)
            if last is not None and (now - last) > self._downtime_threshold:
                await repo.record_downtime(s, down_from=last, down_to=now, reason="missed_cycles")
                log.warning("accrual downtime recorded", down_from=str(last), down_to=str(now))
            await repo.set_heartbeat(s, now)
            await s.commit()

    async def run_cycle(self) -> None:
        now = self._now()
        today = self._today()
        last_closed = today - timedelta(days=1)
        current_month = month_start(today)
        # Fresh per-cycle MNT rate cache (see _get_mnt_rate): fetched at most once
        # for the whole cycle, never reused across cycles (the price moves).
        self._mnt_rate = None
        self._mnt_rate_failed = False

        # Downtime bookkeeping first: a gap since the last heartbeat means the
        # service was down, which later grants expiry grace (business §6.2).
        await self._check_downtime(now)

        # Short read-only session: snapshot the grouping, then close it. Only
        # plain scalar fields are kept (see _AccountRef) so nothing detached
        # leaks into the per-user sessions opened below.
        async with self._sm() as session:
            accounts_by_user_orm = await repo.active_accounts_by_user(session)
            accounts_by_user = {
                user_id: [
                    _AccountRef(
                        id=acc.id,
                        exchange=acc.exchange,
                        uid=acc.uid,
                        accrual_since=acc.accrual_since,
                    )
                    for acc in accounts
                ]
                for user_id, accounts in accounts_by_user_orm.items()
            }

        # Eagerly fetch the MNT->USDT rate ONCE, up front, for the whole cycle —
        # before any user is processed — whenever MNT is enabled and at least one
        # account anywhere is Bybit (the only MNT-bearing exchange today). This
        # closes the critical money-loss bug where a per-account, mid-loop rate
        # fetch could fail AFTER a sibling USDT row for the same day was already
        # frozen: freezing USDT alone finalizes the day, which permanently
        # advances past it (see the per-user skip below) and both drops that
        # day's MNT forever and corrupts the day's rank (computed from a partial
        # gross). Knowing success/failure before any freezing happens lets us
        # decide, per user, whether ANY row may be frozen this cycle at all.
        if self._mnt_enabled and any(
            acc.exchange == "bybit" for accounts in accounts_by_user.values() for acc in accounts
        ):
            try:
                bybit_adapter = self._provider.get("bybit")
            except Exception:
                # No "bybit" adapter registered (e.g. keys not configured) —
                # leave the eager fetch untried; each affected user's own
                # per-account provider.get("bybit") call will raise the same
                # error later and be handled (dev-alerted) by the existing
                # per-user try/except below, unchanged from before this fix.
                bybit_adapter = None
            if bybit_adapter is not None:
                await self._get_mnt_rate(bybit_adapter)

        # Each user gets its own transaction: one user's adapter/DB error must
        # not roll back every other user's already-computed work, and must not
        # stall the whole cycle on retry (deterministic ordering would otherwise
        # re-hit the same failing user first, forever).
        for user_id, accounts in accounts_by_user.items():
            if (
                self._mnt_enabled
                and self._mnt_rate_failed
                and any(acc.exchange == "bybit" for acc in accounts)
            ):
                # MNT can't be valued this cycle and this user has MNT exposure:
                # skip ALL of this user's not-yet-finalized days rather than
                # freeze USDT-only rows that would finalize (and permanently
                # corrupt) a day whose MNT can never be backfilled later. The
                # day(s) stay unfinalized and are retried in full next cycle
                # (last_finalized_day is untouched). The rate-fetch failure
                # already dev-alerted once above; no further alert here.
                log.warning("accrual_user_skipped_mnt_rate_unavailable", user_id=user_id)
                continue
            try:
                async with self._sm() as session:
                    await self._process_user(
                        session, user_id, accounts, last_closed, current_month, now
                    )
                    await session.commit()
            except Exception as exc:
                log.exception("accrual_user_failed", user_id=user_id)
                # Silent stop of a user's accrual (bad key, exchange outage) must
                # reach the developer, not just the logs (CLAUDE.md security rule).
                await self._dev_alert(
                    "Accrual failed for a user",
                    f"user {user_id}: {type(exc).__name__}: {exc}",
                )

        # Rank maintenance is isolated from exchange fetching and existing cash
        # entries. Missing affiliate days leave a pending review, not a downgrade.
        try:
            for user_id in await rank_control.refresh_all(self._sm, self._now()):
                await self._dev_alert("Partner rank review failed", f"user {user_id}")
        except Exception as exc:
            log.exception("partner_rank_maintenance_failed")
            await self._dev_alert("Partner rank maintenance failed", type(exc).__name__)

        async with self._sm() as session:
            await repo.expire_aged(session, now)
            await session.commit()

        # Whole-downline rebate snapshot for the admin stats. Fully isolated: it runs
        # last and its own failure must never affect the accrual work above.
        try:
            await self._refresh_downline_snapshot(now, today)
        except Exception:
            log.exception("downline_snapshot_refresh_failed")

    async def _refresh_downline_snapshot(self, now: datetime, today: date) -> None:
        """Sum every configured exchange's WHOLE-downline USDT rebate for the current
        month and persist it as a snapshot the admin stats reads instantly.

        Two robustness measures against unreliable affiliate endpoints (see below):

        * CLOSED DAYS ONLY — the window ends at the close of YESTERDAY, never the
          still-moving current day, mirroring the per-user accrual path (which freezes
          only closed days). On the 1st of a month no day has closed yet, so every
          exchange reports 0 and nothing is polled.
        * MEDIAN SMOOTHING — some affiliate APIs (notably OKX's ``invitee/list``)
          return unstable PRELIMINARY figures the exchange silently revises by 10-20x
          hours later; left raw this made the admin total flap (e.g. 4703 -> 2099 ->
          202 for the same window within one day). Instead of trusting one live
          reading, the reported per-exchange figure is the MEDIAN of the last
          ``_DOWNLINE_HISTORY`` readings (window carried in ``history``): a lone spike
          is outvoted, yet the median still tracks gradual real growth, and it
          self-heals — a value that is the genuine new reality persists and becomes
          the median once it fills the window's majority. A reading the median just
          smoothed away is surfaced in ``guarded`` (+ dev alert) for transparency.
          Payouts are unaffected — they use frozen per-day accruals, not this.

        Best-effort and per-exchange isolated: an exchange that errors is recorded in
        ``errors`` (not raised), one returning ``None`` (no downline endpoint wired) in
        ``unsupported``; the total covers the rest. Amounts are accrued, not
        guaranteed-payable (same caveat the bot shows elsewhere)."""
        m_start = month_start(today)
        date_from = datetime.combine(m_start, time.min, tzinfo=PAYOUT_TZ)
        # Closed days only: end at the last CLOSED day (yesterday), never `now`.
        last_closed = today - timedelta(days=1)
        date_to = datetime.combine(last_closed, time.max, tzinfo=PAYOUT_TZ)
        has_closed_day = last_closed >= m_start

        # Previous readings, usable as a median window only within the SAME month. A
        # month rollover (or a pre-``history`` snapshot) starts the window fresh, so a
        # stale/garbage prior value can never poison the new month's median.
        async with self._sm() as session:
            prev = await repo.get_downline_snapshot(session)
        prev_history: dict[str, object] = {}
        if prev is not None and str(prev.get("month") or "") == m_start.isoformat()[:7]:
            prev_history = prev.get("history") or {}

        per_exchange: dict[str, str] = {}
        history_out: dict[str, list[str]] = {}
        errors: list[str] = []
        unsupported: list[str] = []
        guarded: list[str] = []
        total = Decimal(0)
        for code in self._exchange_codes:
            if not has_closed_day:
                # Nothing settled this month yet (1st of the month): report 0, no poll.
                per_exchange[code] = "0"
                continue
            try:
                adapter = self._provider.get(code)
                amount = await adapter.get_downline_total(date_from, date_to)
            except Exception as exc:
                log.warning("downline_total_failed", exchange=code, error=str(exc))
                errors.append(code)
                continue
            if amount is None:
                unsupported.append(code)
                continue
            if code in _DOWNLINE_SPIKE_GUARD_EXCHANGES:
                window = [*_parse_history(prev_history.get(code)), amount][-_DOWNLINE_HISTORY:]
                history_out[code] = [str(x) for x in window]
                median = _downline_median(window)
                if _is_downline_outlier(median, amount):
                    # LARGE reading (>= FLOOR) that jumped >= FACTOR vs its recent
                    # median: treat as a preliminary/garbage spike and report the
                    # median instead (self-heals once the value persists). OKX's own
                    # ordinary total is below FLOOR, so it too is reported raw.
                    reported = median
                    log.warning(
                        "downline_total_outlier",
                        exchange=code,
                        reported=str(reported),
                        raw=str(amount),
                    )
                    await self._dev_alert(
                        "Downline total spike smoothed",
                        f"{code}: reported median {reported}, raw reading {amount} "
                        f"(>={_DOWNLINE_GUARD_FACTOR}x apart, >=${_DOWNLINE_GUARD_FLOOR}). "
                        "Likely a preliminary/unstable affiliate figure; median holds "
                        "until it persists.",
                    )
                    guarded.append(code)
                else:
                    reported = amount
            else:
                # Every non-OKX exchange is stable — always trust the live reading.
                reported = amount
            per_exchange[code] = str(reported)
            total += reported
        payload = {
            "month": m_start.isoformat()[:7],
            "as_of": now.isoformat(),
            # Last CLOSED day actually covered (what the admin period end should show).
            "through": last_closed.isoformat() if has_closed_day else m_start.isoformat(),
            "per_exchange": per_exchange,
            "history": history_out,
            "errors": errors,
            "unsupported": unsupported,
            "guarded": guarded,
            "total_usdt": str(total),
        }
        async with self._sm() as session:
            await repo.save_downline_snapshot(session, payload)
            await session.commit()

    async def _collect_user_results(
        self,
        accounts: list[_AccountRef],
        start: date,
        last_closed: date,
        finalized: dict[int, dict[date, Decimal]],
        gate_ends: dict[int, datetime | None],
    ) -> dict[tuple[int, date], _FetchedDay]:
        collected: dict[tuple[int, date], _FetchedDay] = {}
        errors: list[ExchangeApiError] = []
        for acc in accounts:
            adapter = self._provider.get(acc.exchange)
            previous_end = gate_ends.get(acc.id)
            for day in iter_days(max(start, acc.accrual_since), last_closed):
                if day in finalized[acc.id]:
                    continue
                query_start, query_end = _day_bounds_payout_tz(day)
                if acc.exchange == "gate" and previous_end is not None:
                    query_start = max(query_start, previous_end + timedelta(seconds=1))
                if query_start > query_end:
                    continue
                try:
                    result = await fetches.get_or_fetch(
                        self._sm,
                        adapter,
                        account_id=acc.id,
                        uid=acc.uid,
                        day=day,
                        query_from=query_start,
                        query_to=query_end,
                        now=self._now(),
                    )
                except ExchangeApiError as exc:
                    errors.append(exc)
                    # Do not hammer the remaining days of an unavailable account;
                    # still collect all other accounts, irrespective of ordering.
                    break
                collected[(acc.id, day)] = _FetchedDay(query_start, query_end, result)
                if acc.exchange == "gate":
                    previous_end = query_end.astimezone(UTC).replace(microsecond=0)
        if errors:
            raise errors[0]
        return collected

    async def _process_user(
        self,
        session: AsyncSession,
        user_id: int,
        accounts: list[_AccountRef],
        last_closed: date,
        current_month: date,
        now: datetime,
    ) -> None:
        # Settlement lag: if ANY of this user's exchanges credits commission late
        # (e.g. KuCoin), defer this user's whole recent window by the largest such lag —
        # never freeze a day whose data hasn't settled. Whole-user (not per-account) so a
        # day is never frozen only partially, which would corrupt that day's rank. No
        # money is lost: availability is still day+1, the day just freezes a few days later.
        # provider.get is assumed resolvable for every account's exchange (accounts only
        # exist for configured exchanges), exactly as the per-day loop below relies on it;
        # a genuinely missing adapter raises and is handled by run_cycle's per-user guard.
        user_lag = max(
            (
                getattr(self._provider.get(acc.exchange), "settlement_lag_days", 0)
                for acc in accounts
            ),
            default=0,
        )
        last_closed = last_closed - timedelta(days=user_lag)

        # Earliest day needing work across the user's accounts (clamped to epoch).
        starts: list[date] = []
        for acc in accounts:
            last_fin = await repo.last_finalized_day(session, acc.id)
            nxt = (last_fin + timedelta(days=1)) if last_fin else acc.accrual_since
            starts.append(max(nxt, self._epoch, acc.accrual_since))
        if not starts:
            return
        start = min(starts)
        if start > last_closed:
            return

        # Per account, the days in [start, last_closed] already frozen by an
        # earlier cycle (possible because `start` is the MIN across accounts —
        # a sibling account that's further ahead already finalized rows here).
        # Those amounts are reused verbatim; the adapter is never re-polled and
        # the frozen row is never re-upserted, preserving the freeze guarantee.
        finalized_by_acc: dict[int, dict[date, Decimal]] = {
            acc.id: await repo.finalized_amounts(
                session, acc.id, start, last_closed + timedelta(days=1)
            )
            for acc in accounts
        }
        gate_query_ends = {
            acc.id: await repo.last_gate_query_end(session, acc.id)
            for acc in accounts
            if acc.exchange == "gate"
        }
        collected = await self._collect_user_results(
            accounts, start, last_closed, finalized_by_acc, gate_query_ends
        )

        override_pct, override_since = await repo.get_commission_override(session, user_id)

        month: date | None = None
        carry1 = carry2 = 0
        cum_before = Decimal(0)

        for d in iter_days(start, last_closed):
            m = month_start(d)
            if m != month:
                # Close the month we just finished BEFORE reading carry-over for
                # the new one. Without this, a single cycle that spans a month
                # boundary (e.g. a backfill after downtime) would read the
                # carry floor before RankHistory for the prior month exists —
                # silently dropping the carry-over for that cycle. current_month
                # is never closed (still in progress); it also can never be the
                # month we're closing here, since the day range never continues
                # past it.
                if month is not None and month != current_month:
                    await self._close_one_month(session, user_id, month)
                month = m
                carry1 = await repo.get_achieved_rank(session, user_id, prev_month_start(m)) or 0
                carry2 = (
                    await repo.get_achieved_rank(
                        session, user_id, prev_month_start(prev_month_start(m))
                    )
                    or 0
                )
                # Seed cumulative from rows already persisted earlier in this month.
                cum_before = await repo.user_month_gross(session, user_id, m, d)

            # Per (account, asset): native amount, USDT-equivalent, fx rate applied.
            per_account: list[tuple[int, str, Decimal, Decimal, Decimal, int, str | None]] = []
            query_bounds_by_acc: dict[int, tuple[datetime, datetime]] = {}
            day_amount = Decimal(0)
            for acc in accounts:
                if acc.accrual_since > d:
                    continue
                reused = finalized_by_acc[acc.id].get(d)
                if reused is not None:
                    # Already frozen (possibly as two rows, USDT + MNT — see
                    # repo.finalized_amounts, which sums across assets): fold into
                    # the day's cumulative but do not poll or re-upsert — the
                    # existing row(s) stay untouched.
                    day_amount += reused
                    continue
                adapter = self._provider.get(acc.exchange)
                fetched = collected.get((acc.id, d))
                if fetched is None:
                    continue
                if acc.exchange == "gate":
                    # Preserve the old UTC coverage at cutover; never count its last
                    # eight hours again in the first UTC+8 day after deployment.
                    query_bounds_by_acc[acc.id] = (
                        fetched.query_from.astimezone(UTC).replace(microsecond=0),
                        fetched.query_to.astimezone(UTC).replace(microsecond=0),
                    )
                result = fetched.result

                # Sum same-asset lines first (an adapter may emit several lines per
                # asset split by source, e.g. SPOT/FUTURES) so each asset freezes
                # as exactly one row instead of the conflict-upsert clobbering all
                # but the last line.
                native_by_asset: dict[str, Decimal] = {}
                if result.total_usdt is not None:
                    native_by_asset["USDT"] = result.total_usdt
                else:
                    for line in result.lines:
                        if line.asset in ("USDT", "MNT"):
                            native_by_asset[line.asset] = (
                                native_by_asset.get(line.asset, Decimal(0)) + line.amount
                            )
                        # Other assets (e.g. BitMart's BTC/ETH lines) are
                        # intentionally not frozen here — unchanged prior behavior.

                for asset, native in native_by_asset.items():
                    if asset == "USDT":
                        per_account.append(
                            (
                                acc.id,
                                "USDT",
                                native,
                                native,
                                Decimal(1),
                                result.raw_records_count,
                                result.settlement_note,
                            )
                        )
                        day_amount += native
                    elif asset == "MNT":
                        if not self._mnt_enabled:
                            continue
                        rate = await self._get_mnt_rate(adapter)
                        if rate is None:
                            # Rate unavailable this cycle (fetch failed or the
                            # adapter has no mnt_usdt_rate): never freeze MNT
                            # without a real rate. USDT above is unaffected.
                            continue
                        usdt_equiv = native * rate
                        per_account.append(
                            (
                                acc.id,
                                "MNT",
                                native,
                                usdt_equiv,
                                rate,
                                result.raw_records_count,
                                result.settlement_note,
                            )
                        )
                        day_amount += usdt_equiv

            rank = day_effective_rank(cum_before, day_amount, carry1, carry2)
            born_expired = expires_at(d) <= now
            state = "expired" if born_expired else "available"

            for acc_id, asset, native, usdt_equiv, fx, raw_count, note in per_account:
                bounds = query_bounds_by_acc.get(acc_id)
                if rank >= 1:
                    day_override = (
                        override_pct
                        if (
                            override_since is not None
                            and override_pct is not None
                            and d >= override_since
                        )
                        else None
                    )
                    fee = effective_fee(rank, day_override)
                    commission = usdt_equiv * fee
                    net = usdt_equiv - commission
                    net_native = native - native * fee
                else:  # only reachable when amount == 0
                    fee = Decimal(0)
                    commission = Decimal(0)
                    net = usdt_equiv
                    net_native = native
                await repo.upsert_accrual(
                    session,
                    user_id=user_id,
                    account_id=acc_id,
                    accrual_date=d,
                    asset=asset,
                    amount_usdt=usdt_equiv,
                    amount_native=native,
                    net_native=net_native,
                    fx_rate=fx,
                    effective_rank=rank,
                    fee_pct=fee,
                    commission_usdt=commission,
                    net_usdt=net,
                    state=state,
                    available_at=available_at(d),
                    expires_at=expires_at(d),
                    raw_records_count=raw_count,
                    adapter_notes=note,
                    query_from=bounds[0] if bounds else None,
                    query_to=bounds[1] if bounds else None,
                )
            cum_before += day_amount

        await session.flush()
        await self._close_past_months(session, user_id, start, last_closed, current_month)

    async def _close_one_month(self, session: AsyncSession, user_id: int, m: date) -> None:
        """Persist ``m``'s achieved rank (RankHistory). No money is destroyed here.

        A sub-$10 month is NOT burned: its rows stay ``available`` and are simply
        never withdrawable while the user's effective rank is 0 (the withdrawal
        rank-gate + $10 minimum enforce "no payout to dust earners"), then expire
        on their own 30-day schedule (§3.1). This avoids destroying a light month's
        rebate for a user whose carry-over rank still lets them withdraw it (the
        rank frozen per day already reflects the achieved rank; see rank_timing).

        Idempotent: a re-close only bumps the recorded rank up / refreshes gross
        (see repo.write_or_bump_rank_history)."""
        nxt = next_month_start(m)
        gross = await repo.user_month_gross(session, user_id, m, nxt)
        await repo.write_or_bump_rank_history(
            session,
            user_id=user_id,
            period_month=m,
            achieved_rank=rank_for_gross(gross),
            gross_month_usdt=gross,
        )

    async def _close_past_months(
        self,
        session: AsyncSession,
        user_id: int,
        start: date,
        last_closed: date,
        current_month: date,
    ) -> None:
        # Months already closed inline by the day-loop (see _process_user) are
        # safely re-closed here (idempotent — see _close_one_month) so the last
        # month in range, which the loop has no "next month" transition to
        # trigger on, is guaranteed to get closed too.
        m = month_start(start)
        while m <= month_start(last_closed):
            if m == current_month:  # in-progress month: never close it
                break
            await self._close_one_month(session, user_id, m)
            m = next_month_start(m)
        await session.flush()
