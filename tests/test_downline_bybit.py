"""Tests for BybitAdapter.get_downline_total (whole-affiliate-downline USDT sum)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.bybit import BybitAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings


def _creds(code: str) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code],
        SecretStr("api-key"),
        SecretStr("api-secret"),
        None,
    )


async def test_bybit_downline_total_sums_usdt_across_all_users(httpx_mock):
    # aff-user-list, single page: two referrals, each with a per-coin
    # commissionsVol map. Only the USDT entries should be summed; the non-USDT
    # coin (BTC) must be ignored entirely (unlike get_commission, there is no
    # note about ignored coins here — get_downline_total returns a bare total).
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "userId": "111",
                        "commissionsVol": {"USDT": "1.5", "BTC": "0.0001"},
                    },
                    {
                        "userId": "222",
                        "commissionsVol": {"USDT": "0.83421325", "BTC": "0"},
                    },
                ],
                "nextPageCursor": "",
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    total = await adapter.get_downline_total(
        day_start(date(2026, 6, 1)), day_end(date(2026, 6, 30))
    )
    await adapter.aclose()

    assert total == Decimal("1.5") + Decimal("0.83421325")
