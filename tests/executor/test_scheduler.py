from __future__ import annotations

from datetime import datetime

import pytest

from bot.executor.scheduler import ExecutorScheduler


class _CountingExecutor:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.reconcile_calls = 0
        self.process_next_calls = 0
        self.poll_confirmations_calls = 0
        self._fail_on = fail_on or set()
        self._cycle = 0

    async def reconcile(self) -> None:
        # Mirror the real executor: reconcile drives poll_confirmations at its end.
        self.reconcile_calls += 1
        await self.poll_confirmations()

    async def process_next(self) -> None:
        self._cycle += 1
        self.process_next_calls += 1
        if self._cycle in self._fail_on:
            raise RuntimeError("boom")

    async def poll_confirmations(self) -> None:
        self.poll_confirmations_calls += 1


async def _fake_sleep_factory():
    calls = {"count": 0}

    async def _sleep(seconds: float) -> None:
        calls["count"] += 1

    return _sleep, calls


def _now() -> datetime:
    return datetime(2026, 7, 13)


@pytest.mark.asyncio(loop_scope="session")
async def test_run_forever_reconciles_every_tick_and_loops_max_cycles():
    executor = _CountingExecutor()
    sleep_calls = {"count": 0}

    async def sleep(seconds: float) -> None:
        sleep_calls["count"] += 1

    scheduler = ExecutorScheduler(executor, sleep=sleep, now=_now, interval_seconds=1.0)

    cycles = await scheduler.run_forever(max_cycles=3)

    assert cycles == 3
    # reconcile now runs EVERY tick (Fix 1), so a stuck 'submitting' self-heals
    # without a restart — not just once at startup.
    assert executor.reconcile_calls == 3
    assert executor.process_next_calls == 3
    assert executor.poll_confirmations_calls == 3  # driven by reconcile
    assert sleep_calls["count"] == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_run_forever_survives_a_raising_process_next():
    executor = _CountingExecutor(fail_on={2})
    sleep_calls = {"count": 0}

    async def sleep(seconds: float) -> None:
        sleep_calls["count"] += 1

    scheduler = ExecutorScheduler(executor, sleep=sleep, now=_now, interval_seconds=1.0)

    cycles = await scheduler.run_forever(max_cycles=3)

    assert cycles == 3
    # process_next raised on cycle 2, but it's in its own try — reconcile (and its
    # poll_confirmations) still ran all 3 ticks, and the loop completed.
    assert executor.reconcile_calls == 3
    assert executor.process_next_calls == 3
    assert executor.poll_confirmations_calls == 3
    assert sleep_calls["count"] == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_run_forever_survives_reconcile_failure_without_blocking_process_next():
    class _FailingReconcile(_CountingExecutor):
        async def reconcile(self) -> None:
            self.reconcile_calls += 1
            raise RuntimeError("reconcile boom")  # raises BEFORE poll_confirmations

    executor = _FailingReconcile()

    async def sleep(seconds: float) -> None:
        return None

    scheduler = ExecutorScheduler(executor, sleep=sleep, now=_now, interval_seconds=1.0)

    cycles = await scheduler.run_forever(max_cycles=2)

    assert cycles == 2
    assert executor.reconcile_calls == 2  # attempted every tick
    # KEY: a failing reconcile (its own try) never blocks process_next.
    assert executor.process_next_calls == 2
    assert executor.poll_confirmations_calls == 0  # reconcile raised before reaching it
