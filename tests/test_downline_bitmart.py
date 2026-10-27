"""Tests for BitmartAdapter.get_downline_total (whole-downline USDT rebate)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.bitmart import BitmartAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_DAY = date(2026, 5, 1)


def _creds(code: str) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code], SecretStr("api-key"), SecretStr("api-secret"), None
    )


async def test_bitmart_downline_sums_usdt_across_whole_affiliate_list(httpx_mock):
    # No user_id filter -> BitMart returns every referral's rows. Two invitees'
    # USDT rows must sum; a stray non-USDT row (defensive - shouldn't occur since
    # currency=USDT is requested, but the adapter must not count it if it did).
    httpx_mock.add_response(
        json={
            "total": 3,
            "size": 50,
            "page": 1,
            "usdt_rebate_sum": "37.25",
            "rebate_detail_page_data": [
                {
                    "rebate_coin": "USDT",
                    "trade_user_id": 111,
                    "total_rebate_amount": "12.5",
                },
                {
                    "rebate_coin": "USDT",
                    "trade_user_id": 222,
                    "total_rebate_amount": "24.75",
                },
                {
                    "rebate_coin": "BTC",
                    "trade_user_id": 333,
                    "total_rebate_amount": "0.001",
                },
            ],
        }
    )
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal("37.25")

    request = httpx_mock.get_requests()[0]
    qs = parse_qs(urlparse(str(request.url)).query)
    assert "user_id" not in qs
    assert qs["currency"][0] == "USDT"


async def test_bitmart_downline_paginates_and_sums_usdt(httpx_mock):
    # size=1 forces two pages; both pages' USDT rows must be summed.
    from bot.exchanges import bitmart as bitmart_module

    original_page_limit = bitmart_module._PAGE_LIMIT
    bitmart_module._PAGE_LIMIT = 1
    try:
        httpx_mock.add_response(
            json={
                "total": 2,
                "size": 1,
                "page": 1,
                "rebate_detail_page_data": [
                    {"rebate_coin": "USDT", "trade_user_id": 111, "total_rebate_amount": "1.0"}
                ],
            }
        )
        httpx_mock.add_response(
            json={
                "total": 2,
                "size": 1,
                "page": 2,
                "rebate_detail_page_data": [
                    {"rebate_coin": "USDT", "trade_user_id": 222, "total_rebate_amount": "2.5"}
                ],
            }
        )
        adapter = BitmartAdapter(_creds("bitmart"), make_settings())
        total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
        await adapter.aclose()
        assert total == Decimal("3.5")
    finally:
        bitmart_module._PAGE_LIMIT = original_page_limit


async def test_bitmart_downline_falls_back_to_top_level_sum_when_no_rows(httpx_mock):
    # No detail rows but a top-level usdt_rebate_sum -> use that instead of 0.
    httpx_mock.add_response(
        json={
            "total": 0,
            "size": 50,
            "page": 1,
            "usdt_rebate_sum": "5.5",
            "rebate_detail_page_data": [],
        }
    )
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert total == Decimal("5.5")


async def test_bitmart_downline_empty_returns_zero_not_none(httpx_mock):
    httpx_mock.add_response(json={"total": 0, "size": 50, "page": 1, "rebate_detail_page_data": []})
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert total == Decimal("0")
