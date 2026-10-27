"""Bybit public MNTUSDT price feed (no auth) — used to value MNT rebate in USDT."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.exchanges.base import ExchangeApiError
from bot.exchanges.bybit import BybitAdapter
from tests.conftest import make_settings
from tests.test_adapters import _creds


async def test_mnt_usdt_rate_parses_last_price(httpx_mock):
    httpx_mock.add_response(
        url="https://api.bybit.com/v5/market/tickers?category=spot&symbol=MNTUSDT",
        json={"retCode": 0, "result": {"list": [{"symbol": "MNTUSDT", "lastPrice": "0.5321"}]}},
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    assert await adapter.mnt_usdt_rate() == Decimal("0.5321")
    await adapter.aclose()


async def test_mnt_usdt_rate_request_is_public_unsigned(httpx_mock):
    # No X-BAPI-* signing headers — this endpoint is public and must not be signed.
    httpx_mock.add_response(
        json={"retCode": 0, "result": {"list": [{"symbol": "MNTUSDT", "lastPrice": "0.5"}]}}
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    await adapter.mnt_usdt_rate()
    await adapter.aclose()

    request = httpx_mock.get_requests()[0]
    assert "X-BAPI-SIGN" not in request.headers
    assert request.url.path == "/v5/market/tickers"


async def test_mnt_usdt_rate_raises_on_missing_price(httpx_mock):
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": []}})
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    with pytest.raises(ExchangeApiError):
        await adapter.mnt_usdt_rate()
    await adapter.aclose()


async def test_mnt_usdt_rate_raises_on_empty_price_string(httpx_mock):
    httpx_mock.add_response(
        json={"retCode": 0, "result": {"list": [{"symbol": "MNTUSDT", "lastPrice": ""}]}}
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    with pytest.raises(ExchangeApiError):
        await adapter.mnt_usdt_rate()
    await adapter.aclose()
