"""Bybit adapter must expose MNT commission as its own line (alongside USDT)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from bot.exchanges.bybit import BybitAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings
from tests.test_adapters import _creds


async def test_bybit_reports_usdt_and_mnt_lines(httpx_mock):
    # commissionsVol carries USDT and MNT commission plus a zero BTC entry; BTC must
    # stay ignored while both USDT and MNT become their own reported lines.
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "userId": "553515896",
                        "commissionsVol": {"USDT": "0.5", "MNT": "100", "BTC": "0"},
                    }
                ],
                "nextPageCursor": "",
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission(
        "553515896", day_start(date(2026, 6, 1)), day_end(date(2026, 6, 30))
    )
    await adapter.aclose()

    amounts = {line.asset: line.amount for line in result.lines}
    assert amounts["USDT"] == Decimal("0.5")
    assert amounts["MNT"] == Decimal("100")
    assert "BTC" not in amounts
