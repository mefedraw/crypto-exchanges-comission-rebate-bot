"""Minimal periodic driver for the payout executor. Mirrors
`bot.accrual.scheduler.AccrualScheduler`.

Every tick runs `reconcile()` THEN `process_next()`:

* `reconcile()` first — it resolves any in-flight `submitting` payout (promoting a tx
  that landed but whose broadcast raised `ChainError`) and drives `sent` -> `paid` (it
  calls `poll_confirmations()` itself). Running it every tick, not only at startup,
  means a broadcast-uncertain payout self-heals without a process restart, and the
  single-flight guard unclogs so the next payout can proceed.
* `process_next()` second — claims and sends the next `reserved` payout.

Each is wrapped in its own try/except so a transient reconcile error never skips
process_next (and vice versa). Clock and sleep are injected so tests run instantly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class _Executor(Protocol):
    async def reconcile(self) -> None: ...
    async def process_next(self) -> None: ...
    async def poll_confirmations(self) -> None: ...


class ExecutorScheduler:
    def __init__(
        self,
        executor: _Executor,
        *,
        sleep: Callable[[float], Awaitable[None]],
        now: Callable[[], datetime],
        interval_seconds: float = 30.0,
    ) -> None:
        self._executor = executor
        self._sleep = sleep
        self._now = now
        self._interval = interval_seconds

    async def run_forever(self, max_cycles: int | None = None) -> int:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            # reconcile first: resolve/finalize in-flight payouts (heals a stuck
            # 'submitting' every tick, not only at startup) BEFORE claiming a new one.
            try:
                await self._executor.reconcile()
            except Exception:  # a transient reconcile error must not block process_next
                log.exception("executor_reconcile_failed")
            try:
                await self._executor.process_next()
            except Exception:  # a bad send must not kill the loop
                log.exception("executor_process_failed")
            cycles += 1
            await self._sleep(self._interval)
        return cycles
