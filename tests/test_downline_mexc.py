"""Tests for MexcAdapter.get_downline_total (whole-downline USDT rebate)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.mexc import MexcAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_FROM = day_start(date(2026, 5, 1))
_TO = day_end(date(2026, 5, 31))


def _creds(code: str) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code],
        SecretStr("api-key"),
        SecretStr("api-secret"),
        None,
    )


async def test_mexc_downline_total_sums_all_invitees_across_pages(httpx_mock):
    # Live-shaped response: no `uid` filter, rows for MULTIPLE invitees come back;
    # `total` (USDT) is summed across every row, across pages.
    httpx_mock.add_response(
        json={
            "code": 0,
            "data": {
                "totalPage": 2,
                "resultList": [
                    {"uid": "43305891", "total": "5.0"},
                    {"uid": "99999999", "total": "3.25"},
                ],
            },
        }
    )
    httpx_mock.add_response(
        json={
            "code": 0,
            "data": {
                "totalPage": 2,
                "resultList": [{"uid": "11112222", "total": "1.75"}],
            },
        }
    )
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    total = await adapter.get_downline_total(_FROM, _TO)
    await adapter.aclose()

    assert total == Decimal("10.0")
    assert len(httpx_mock.get_requests()) == 2  # stopped at totalPage


async def test_mexc_downline_total_omits_uid_param(httpx_mock):
    httpx_mock.add_response(json={"code": 0, "data": {"totalPage": 1, "resultList": []}})
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    await adapter.get_downline_total(_FROM, _TO)
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert "uid" not in qs


async def test_mexc_downline_total_zero_when_no_rows(httpx_mock):
    httpx_mock.add_response(json={"code": 0, "data": {"totalPage": 1, "resultList": []}})
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    total = await adapter.get_downline_total(_FROM, _TO)
    await adapter.aclose()

    assert total == Decimal(0)
