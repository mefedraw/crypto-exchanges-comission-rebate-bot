"""Production-only operator entry point for an authorized, precomputed Gate credit.

Run inside the deployed bot environment; the local calculator has a different DB
layout. A reviewed JSON plan is required and no exchange or withdrawal API is called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

from bot.accrual.corrections import GateCorrectionPlan, post_gate_correction
from bot.botui.service import balance_summary
from bot.db.engine import make_engine, make_sessionmaker
from bot.db.settings import DbSettings
from bot.payout.expiry import PAYOUT_TZ


def load_plan(payload: object) -> GateCorrectionPlan:
    if not isinstance(payload, dict):
        raise ValueError("Plan must be a JSON object.")
    required = {
        "correction_key",
        "account_id",
        "user_id",
        "uid",
        "period_month",
        "source_from",
        "source_to",
        "expected_gross",
        "expected_net",
        "basis_sha256",
        "effective_rank",
    }
    if set(payload) != required:
        raise ValueError("Plan fields are incomplete or unknown.")
    for field in ("expected_gross", "expected_net"):
        if not isinstance(payload[field], str):
            raise ValueError("Money must be supplied as decimal strings, never JSON floats.")
    return GateCorrectionPlan(
        correction_key=str(payload["correction_key"]),
        account_id=int(payload["account_id"]),
        user_id=int(payload["user_id"]),
        uid=str(payload["uid"]),
        period_month=date.fromisoformat(str(payload["period_month"])),
        source_from=datetime.fromisoformat(str(payload["source_from"])),
        source_to=datetime.fromisoformat(str(payload["source_to"])),
        expected_gross=Decimal(payload["expected_gross"]),
        expected_net=Decimal(payload["expected_net"]),
        basis_sha256=str(payload["basis_sha256"]),
        effective_rank=int(payload["effective_rank"]),
    )


async def run(plan: GateCorrectionPlan, actor: str) -> dict[str, Any]:
    engine = make_engine(DbSettings().database_url)
    sm = make_sessionmaker(engine)
    now = datetime.now(UTC)
    try:
        async with sm() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            receipt = await post_gate_correction(session, plan, actor=actor, now=now)
        async with sm() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            summary = await balance_summary(
                session, plan.user_id, now, now.astimezone(PAYOUT_TZ).date()
            )
        return {
            **asdict(receipt),
            "correction_key": plan.correction_key,
            "available_balance_usdt": str(summary.available_net),
            "effective_rank": summary.effective_rank,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="JSON plan path, or - for stdin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor", default="operator:owner-request")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required to post an explicitly authorized correction")
    if args.plan == "-":
        payload = json.load(sys.stdin)
    else:
        with Path(args.plan).open(encoding="utf-8-sig") as source:
            payload = json.load(source)
    result = asyncio.run(run(load_plan(payload), args.actor))
    print(json.dumps(result, default=str, ensure_ascii=True))


if __name__ == "__main__":
    main()
