"""Tests for BinanceAdapter.get_downline_total (whole-downline USDT rebate)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.binance import BinanceAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

# <= 7 days so the whole range is a single window (one request).
_FROM = day_start(date(2026, 5, 1))
_TO = day_end(date(2026, 5, 5))


def _creds(code: str) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code], SecretStr("api-key"), SecretStr("api-secret"), None
    )


async def test_binance_downline_total_sums_only_usdt(httpx_mock):
    # No customerId filter -> every invitee's records; only the USDT `income` is summed.
    httpx_mock.add_response(
        json=[
            {"customerId": "111", "income": "5.0", "asset": "USDT"},
            {"customerId": "222", "income": "0.1", "asset": "BNB"},  # ignored (not USDT)
            {"customerId": "333", "income": "3.25", "asset": "USDT"},
        ]
    )
    adapter = BinanceAdapter(_creds("binance"), make_settings())
    total = await adapter.get_downline_total(_FROM, _TO)
    await adapter.aclose()
    assert total == Decimal("8.25")


async def test_binance_downline_total_omits_customer_id(httpx_mock):
    httpx_mock.add_response(json=[])
    adapter = BinanceAdapter(_creds("binance"), make_settings())
    total = await adapter.get_downline_total(_FROM, _TO)
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert "customerId" not in qs
    assert total == Decimal(0)
