# tests/accrual/test_downline_snapshot.py
"""Whole-downline rebate snapshot: storage roundtrip + the worker's refresh step."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.accrual import repository as acc_repo
from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.worker import AccrualWorker
from bot.exchanges.base import CommissionResult, ExchangeAdapter

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


class _DownlineAdapter(ExchangeAdapter):
    """Scripted get_downline_total: a Decimal, None (unsupported), or raises.

    Also records the (date_from, date_to) it was last called with, so tests can
    assert the worker queries only CLOSED days (up to yesterday, never `now`)."""

    def __init__(self, code: str, value: Decimal | None | str) -> None:
        self.name = code
        self.code = code
        self._value = value
        self.last_range: tuple[datetime, datetime] | None = None

    async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
        return CommissionResult(exchange=self.code, uid=uid, date_from=date_from, date_to=date_to)

    async def get_downline_total(self, date_from, date_to):  # type: ignore[override]
        self.last_range = (date_from, date_to)
        if self._value == "raise":
            raise RuntimeError("adapter boom")
        return self._value


async def test_downline_snapshot_roundtrip(sessionmaker):
    async with sessionmaker() as s:
        assert await acc_repo.get_downline_snapshot(s) is None
        await acc_repo.save_downline_snapshot(s, {"total_usdt": "5", "per_exchange": {"gate": "5"}})
        await s.commit()
    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["total_usdt"] == "5"
    assert snap["per_exchange"] == {"gate": "5"}


async def test_worker_refresh_downline_snapshot_sums_and_classifies(sessionmaker):
    """A supported exchange contributes to the total; None -> unsupported; raise -> errors."""
    provider = MappingAdapterProvider(
        {
            "gate": _DownlineAdapter("gate", Decimal("400.5")),
            "mexc": _DownlineAdapter("mexc", Decimal("100")),
            "okx": _DownlineAdapter("okx", None),  # no downline endpoint wired
            "weex": _DownlineAdapter("weex", "raise"),  # API error
        }
    )
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("gate", "mexc", "okx", "weex")

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["total_usdt"] == "500.5"
    assert snap["per_exchange"] == {"gate": "400.5", "mexc": "100"}
    assert snap["unsupported"] == ["okx"]
    assert snap["errors"] == ["weex"]
    assert snap["month"] == "2026-07"


async def test_worker_refresh_never_raises_on_total_failure(sessionmaker):
    """Snapshot refresh is best-effort — an all-failing set still saves a zero total."""
    provider = MappingAdapterProvider({"weex": _DownlineAdapter("weex", "raise")})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("weex",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["total_usdt"] == "0"
    assert snap["errors"] == ["weex"]
    assert snap["per_exchange"] == {}


async def test_worker_refresh_queries_closed_days_only(sessionmaker):
    """The whole-downline query must cover [month start .. end of YESTERDAY], never the
    still-moving current day — mirroring the per-user accrual (closed days only)."""
    gate = _DownlineAdapter("gate", Decimal("10"))
    provider = MappingAdapterProvider({"gate": gate})
    worker = AccrualWorker(
        sessionmaker, provider, now=lambda: datetime(2026, 7, 21, 6, 30, tzinfo=UTC)
    )
    worker._exchange_codes = ("gate",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 21))

    assert gate.last_range is not None
    date_from, date_to = gate.last_range
    # month start (in PAYOUT_TZ) .. end of the last CLOSED day (the 20th), not the 21st.
    assert date_from.date() == date(2026, 7, 1)
    assert date_to.date() == date(2026, 7, 20)
    assert date_to.hour == 23 and date_to.minute == 59


async def test_worker_refresh_first_of_month_reports_zero(sessionmaker):
    """On the 1st, no day of the current month has closed yet -> every exchange is 0
    and the flaky affiliate endpoints are not even polled."""
    gate = _DownlineAdapter("gate", Decimal("999"))
    provider = MappingAdapterProvider({"gate": gate})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 1, 6, tzinfo=UTC))
    worker._exchange_codes = ("gate",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 1))

    assert gate.last_range is None  # never polled
    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["total_usdt"] == "0"
    assert snap["per_exchange"] == {"gate": "0"}


async def test_worker_refresh_median_smooths_spike(sessionmaker):
    """A lone spike is outvoted by the median of the last readings: with a window of
    [200, 200] the fresh 5000 gives median([200, 200, 5000]) = 200. The spike is
    surfaced in `guarded`. This is the OKX preliminary-figure defense."""
    async with sessionmaker() as s:
        await acc_repo.save_downline_snapshot(
            s,
            {
                "month": "2026-07",
                "per_exchange": {"okx": "200"},
                "history": {"okx": ["200", "200"]},
                "total_usdt": "200",
            },
        )
        await s.commit()

    okx = _DownlineAdapter("okx", Decimal("5000"))  # 25x spike -> garbage
    provider = MappingAdapterProvider({"okx": okx})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("okx",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["per_exchange"] == {"okx": "200"}  # median, spike smoothed away
    assert snap["total_usdt"] == "200"
    assert snap["guarded"] == ["okx"]
    assert snap["history"]["okx"] == ["200", "200", "5000"]  # window keeps last 3


async def test_worker_refresh_self_heals_from_poisoned_prev(sessionmaker):
    """A pre-`history` snapshot (garbage per_exchange, no window) can NOT poison the
    new reading: with an empty window the first reading is reported verbatim, so a
    trustworthy fresh value shows immediately rather than being held back."""
    async with sessionmaker() as s:
        await acc_repo.save_downline_snapshot(
            s, {"month": "2026-07", "per_exchange": {"okx": "2099"}, "total_usdt": "2099"}
        )
        await s.commit()

    okx = _DownlineAdapter("okx", Decimal("202"))  # the real value
    provider = MappingAdapterProvider({"okx": okx})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("okx",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["per_exchange"] == {"okx": "202"}  # clean start, no stale 2099
    assert snap["guarded"] == []
    assert snap["history"]["okx"] == ["202"]


async def test_worker_refresh_median_tracks_persistent_new_value(sessionmaker):
    """Median self-heals: once a genuinely new value fills the window majority it
    becomes the median. Window [200, 900] + fresh 900 -> median([200,900,900]) = 900."""
    async with sessionmaker() as s:
        await acc_repo.save_downline_snapshot(
            s,
            {
                "month": "2026-07",
                "per_exchange": {"okx": "200"},
                "history": {"okx": ["200", "900"]},
                "total_usdt": "200",
            },
        )
        await s.commit()

    okx = _DownlineAdapter("okx", Decimal("900"))
    provider = MappingAdapterProvider({"okx": okx})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("okx",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["per_exchange"] == {"okx": "900"}  # new reality now the median
    assert snap["guarded"] == []


async def test_worker_refresh_ignores_prev_window_across_month_boundary(sessionmaker):
    """A window from a DIFFERENT month is discarded: the new month starts fresh, so a
    fresh reading is reported verbatim (never blended with last month's readings)."""
    async with sessionmaker() as s:
        await acc_repo.save_downline_snapshot(
            s,
            {
                "month": "2026-06",
                "per_exchange": {"okx": "5000"},
                "history": {"okx": ["5000", "5000"]},
                "total_usdt": "5000",
            },
        )
        await s.commit()

    okx = _DownlineAdapter("okx", Decimal("120"))
    provider = MappingAdapterProvider({"okx": okx})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("okx",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["per_exchange"] == {"okx": "120"}  # fresh month -> verbatim
    assert snap["history"]["okx"] == ["120"]
    assert snap["guarded"] == []


async def test_worker_refresh_non_okx_spike_reported_raw(sessionmaker):
    """The spike guard is OKX-only. A huge jump on ANY other exchange (here gate
    200 -> 5000, which WOULD be smoothed if guarded) is reported RAW and never flagged —
    every non-OKX exchange returns stable data and is always trusted."""
    async with sessionmaker() as s:
        await acc_repo.save_downline_snapshot(
            s,
            {
                "month": "2026-07",
                "per_exchange": {"gate": "200"},
                "history": {"gate": ["200", "200"]},
                "total_usdt": "200",
            },
        )
        await s.commit()

    gate = _DownlineAdapter("gate", Decimal("5000"))  # 25x, above floor — still raw
    provider = MappingAdapterProvider({"gate": gate})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("gate",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["per_exchange"] == {"gate": "5000"}  # raw — guard does not apply to gate
    assert snap["guarded"] == []


async def test_worker_refresh_small_real_jump_shows_raw_not_smoothed(sessionmaker):
    """Regression (KuCoin 0.72 -> 61.12): KuCoin is not OKX, so its reading is reported
    RAW, never smoothed to the stale prior value. The spike guard is OKX-only."""
    async with sessionmaker() as s:
        await acc_repo.save_downline_snapshot(
            s,
            {
                "month": "2026-07",
                "per_exchange": {"kucoin": "0.72"},
                "history": {"kucoin": ["0.72", "0.72"]},
                "total_usdt": "0.72",
            },
        )
        await s.commit()

    kucoin = _DownlineAdapter("kucoin", Decimal("61.12"))  # 85x jump but only $61
    provider = MappingAdapterProvider({"kucoin": kucoin})
    worker = AccrualWorker(sessionmaker, provider, now=lambda: datetime(2026, 7, 20, 4, tzinfo=UTC))
    worker._exchange_codes = ("kucoin",)

    await worker._refresh_downline_snapshot(worker._now(), date(2026, 7, 20))

    async with sessionmaker() as s:
        snap = await acc_repo.get_downline_snapshot(s)
    assert snap is not None
    assert snap["per_exchange"] == {"kucoin": "61.12"}  # real money shown at once
    assert snap["total_usdt"] == "61.12"
    assert snap["guarded"] == []  # NOT flagged, NOT held back
