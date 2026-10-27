"""Minimal daily driver for the accrual worker. The heavy lifting is idempotent
(AccrualWorker.run_cycle catches up on missed days), so the scheduler just fires a
cycle on start and then every `interval_seconds`. Clock and sleep are injected so
tests run instantly and deterministically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog

from bot.accrual.worker import AccrualWorker

log = structlog.get_logger(__name__)


class AccrualScheduler:
    def __init__(
        self,
        worker: AccrualWorker,
        *,
        sleep: Callable[[float], Awaitable[None]],
        now: Callable[[], datetime],
        interval_seconds: float = 86400.0,
    ) -> None:
        self._worker = worker
        self._sleep = sleep
        self._now = now
        self._interval = interval_seconds

    async def run_forever(self, max_cycles: int | None = None) -> int:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                await self._worker.run_cycle()
            except Exception as exc:  # a bad cycle must not kill the loop
                log.exception("accrual_cycle_failed")
                await self._worker.report_cycle_failure(exc)
            cycles += 1
            await self._sleep(self._interval)
        return cycles
