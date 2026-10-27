"""Accrual-worker process entrypoint: ``python -m bot.accrual``.

Runs the daily accrual cycle (poll each active exchange account for the closed day,
freeze rank/fee/commission, close months, expire aged rows). Separate process from the
bot and the executor. Read-only w.r.t. exchanges + the accrual/rank tables; holds no
payout key and no Telegram token.

Thin runtime wiring (like ``bot.main.run`` / ``bot.executor.__main__``); the worker's
logic is unit-tested elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.scheduler import AccrualScheduler
from bot.accrual.worker import AccrualWorker
from bot.config import get_settings
from bot.db.engine import make_engine, make_sessionmaker
from bot.db.settings import DbSettings
from bot.exchanges.registry import build_adapters
from bot.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging()

    adapters = build_adapters(settings)
    logger.info(
        "starting accrual-worker",
        available_exchanges=sorted(adapters),
        rebate_epoch=str(settings.rebate_epoch),
    )
    if not adapters:
        logger.warning("no exchange adapters configured — nothing will accrue")

    engine = make_engine(DbSettings().database_url)
    sessionmaker = make_sessionmaker(engine)
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider(adapters),
        now=lambda: datetime.now(UTC),
        developer_id=settings.developer_telegram_id,
    )
    scheduler = AccrualScheduler(
        worker,
        sleep=asyncio.sleep,
        now=lambda: datetime.now(UTC),
        interval_seconds=settings.accrual_interval_seconds,
    )
    try:
        await scheduler.run_forever()
    finally:
        for adapter in adapters.values():
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("accrual-worker shutdown requested")


if __name__ == "__main__":
    main()
