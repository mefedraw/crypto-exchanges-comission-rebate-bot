"""Manual smoke test for a single exchange adapter against the LIVE API.

Run OUTSIDE of CI. Reads credentials from the environment / .env exactly like the
bot does (this process reads .env; secrets are never printed).

Usage:
    python scripts/smoke.py <exchange_code> <uid> [date_from] [date_to]

Examples:
    python scripts/smoke.py mexc 12345678
    python scripts/smoke.py bitget 12345678 2026-05-01 2026-05-31

Dates are YYYY-MM-DD (UTC). If omitted, the last 7 days are used.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import date, timedelta

# Allow running as a plain file (`python scripts/smoke.py`): put the project root
# on sys.path so the `bot` package is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import get_settings  # noqa: E402
from bot.exchanges.registry import build_adapters
from bot.logging import configure_logging
from bot.rendering import render_result
from bot.utils.dates import day_end, day_start, parse_date


async def _run(code: str, uid: str, date_from: date, date_to: date) -> int:
    settings = get_settings()
    adapters = build_adapters(settings)

    if code not in adapters:
        print(f"Adapter '{code}' is not available.")
        print(f"Available (credentials present): {sorted(adapters) or 'none'}")
        return 2

    adapter = adapters[code]
    print(f"== {adapter.name} | uid={uid} | {date_from} .. {date_to} ==")
    try:
        result = await adapter.get_commission(uid, day_start(date_from), day_end(date_to))
    except Exception:  # noqa: BLE001 - smoke test wants the full traceback
        print("\n--- REQUEST FAILED ---")
        traceback.print_exc()
        return 1
    finally:
        aclose = getattr(adapter, "aclose", None)
        if aclose is not None:
            await aclose()

    print("\n--- RESULT (rendered) ---")
    print(render_result(result))
    print("\n--- RAW ---")
    print(f"records={result.raw_records_count} lines={result.lines} total_usdt={result.total_usdt}")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    code = sys.argv[1].lower()
    uid = sys.argv[2]
    if len(sys.argv) >= 5:
        date_from = parse_date(sys.argv[3])
        date_to = parse_date(sys.argv[4])
    else:
        today = date.today()
        date_from, date_to = today - timedelta(days=7), today - timedelta(days=1)

    configure_logging(json_output=False)
    return asyncio.run(_run(code, uid, date_from, date_to))


if __name__ == "__main__":
    raise SystemExit(main())
