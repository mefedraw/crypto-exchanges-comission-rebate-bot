from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.accrual.scheduler import AccrualScheduler

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _CountingWorker:
    def __init__(self, fail_on: set[int] | None = None):
        self.calls = 0
        self._fail_on = fail_on or set()
        self.reported: list[BaseException] = []

    async def run_cycle(self) -> None:
        self.calls += 1
        if self.calls in self._fail_on:
            raise RuntimeError("boom")

    async def report_cycle_failure(self, exc: BaseException) -> None:
        self.reported.append(exc)


async def test_runs_max_cycles() -> None:
    worker = _CountingWorker()
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)

    sched = AccrualScheduler(
        worker,  # type: ignore[arg-type]
        sleep=fake_sleep,
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
        interval_seconds=3600.0,
    )
    ran = await sched.run_forever(max_cycles=3)
    assert ran == 3
    assert worker.calls == 3
    assert slept == [3600.0, 3600.0, 3600.0]


async def test_cycle_error_does_not_stop_loop() -> None:
    worker = _CountingWorker(fail_on={2})

    async def fake_sleep(sec: float) -> None:
        return None

    sched = AccrualScheduler(
        worker,  # type: ignore[arg-type]
        sleep=fake_sleep,
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    ran = await sched.run_forever(max_cycles=3)
    assert ran == 3  # cycle 2 raised but loop continued
    assert worker.calls == 3
    # the failed cycle was reported to the developer (dev-alert path)
    assert len(worker.reported) == 1
    assert isinstance(worker.reported[0], RuntimeError)
