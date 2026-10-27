"""OKX whole-downline USDT total: invitee/list without the ``keyword`` filter.

Mirrors tests/test_downline_gate.py and the OKX get_commission tests in
tests/test_adapters.py, but asserts every invitee's totalCommission is summed
(no keyword param sent) and that pagination via page/limit is honoured.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.okx import OkxAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_DAY = date(2026, 5, 1)


def _creds(code: str) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code], SecretStr("api-key"), SecretStr("api-secret"), SecretStr("pass")
    )


async def test_okx_downline_total_sums_all_invitees(httpx_mock):
    httpx_mock.add_response(
        json={
            "code": "0",
            "data": [
                {"uid": "111", "totalCommission": "1.5"},
                {"uid": "222", "totalCommission": "2.5"},
            ],
        }
    )
    adapter = OkxAdapter(_creds("okx"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal("4.0")


async def test_okx_downline_total_omits_keyword_filter(httpx_mock):
    httpx_mock.add_response(json={"code": "0", "data": []})
    adapter = OkxAdapter(_creds("okx"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal(0)  # empty downline -> 0, never None
    request = httpx_mock.get_requests()[0]
    params = parse_qs(urlparse(str(request.url)).query)
    assert "keyword" not in params  # whole downline, not one referral
    assert params["periodType"][0] == "custom"


async def test_okx_downline_total_paginates(httpx_mock):
    # A full first page (100 rows) must trigger a second page request.
    first_page = [{"uid": str(i), "totalCommission": "0.1"} for i in range(100)]
    httpx_mock.add_response(json={"code": "0", "data": first_page})
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "999", "totalCommission": "5.0"}]})
    adapter = OkxAdapter(_creds("okx"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    # 100 * 0.1 + 5.0 == 15.0
    assert total == Decimal("15.0")
    assert len(httpx_mock.get_requests()) == 2  # short second page stops pagination


async def test_okx_downline_total_slices_windows_over_90_days(httpx_mock):
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "1", "totalCommission": "10.0"}]})
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "2", "totalCommission": "2.5"}]})
    adapter = OkxAdapter(_creds("okx"), make_settings())
    total = await adapter.get_downline_total(
        day_start(date(2026, 3, 15)), day_end(date(2026, 7, 12))
    )
    await adapter.aclose()

    assert total == Decimal("12.5")
    assert len(httpx_mock.get_requests()) == 2  # 120 days -> 90 + 30
