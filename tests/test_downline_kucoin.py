"""KuCoin get_downline_total against mocked HTTP responses (no live API calls).

Unlike get_commission (queryTransactionByUid, per-invitee), get_downline_total
deliberately calls queryMyCommission with NO uid filter — its aggregate,
whole-affiliate-rebate behavior (userId silently ignored, see bot/exchanges/kucoin.py
module docstring) is exactly what a downline total wants.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.kucoin import KucoinAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_DAY = date(2026, 5, 1)


def _creds() -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE["kucoin"],
        SecretStr("api-key"),
        SecretStr("api-secret"),
        SecretStr("pass"),
    )


async def test_kucoin_downline_total_sums_usdt_across_whole_affiliate(httpx_mock):
    # queryMyCommission returns a bare list under `data` (no data.items wrapper),
    # each record carrying `commission` + `currency`, with NO per-uid field at all —
    # confirmed via current KuCoin docs. Non-USDT currencies must be ignored.
    httpx_mock.add_response(
        json={
            "code": "200000",
            "data": [
                {"commission": "1.5", "currency": "USDT"},
                {"commission": "2.5", "currency": "USDT"},
                {"commission": "0.001", "currency": "BTC"},
            ],
        }
    )
    adapter = KucoinAdapter(_creds(), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal("4.0")


async def test_kucoin_downline_total_hits_query_my_commission_with_no_uid_filter(httpx_mock):
    # Regression: get_downline_total MUST hit queryMyCommission (the aggregate),
    # NOT queryTransactionByUid, and MUST NOT send a uid/userId filter — the whole
    # point is to get every referral's rebate, including unregistered invitees.
    httpx_mock.add_response(json={"code": "200000", "data": []})
    adapter = KucoinAdapter(_creds(), make_settings())
    await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    request = httpx_mock.get_requests()[0]
    url = urlparse(str(request.url))
    assert url.path == "/api/v2/affiliate/queryMyCommission"
    qs = parse_qs(url.query)
    assert "uid" not in qs
    assert "userId" not in qs


async def test_kucoin_downline_total_paginates(httpx_mock):
    # A full first page (== _PAGE_SIZE) must trigger a second page fetch.
    full_page = [{"commission": "1.0", "currency": "USDT"} for _ in range(100)]
    httpx_mock.add_response(json={"code": "200000", "data": full_page})
    httpx_mock.add_response(
        json={"code": "200000", "data": [{"commission": "0.5", "currency": "USDT"}]}
    )
    adapter = KucoinAdapter(_creds(), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    # 100 * 1.0 + 0.5 == 100.5
    assert total == Decimal("100.5")
    assert len(httpx_mock.get_requests()) == 2


async def test_kucoin_downline_total_returns_zero_when_empty(httpx_mock):
    httpx_mock.add_response(json={"code": "200000", "data": []})
    adapter = KucoinAdapter(_creds(), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal(0)


async def test_kucoin_downline_total_uses_plain_utc_window(httpx_mock):
    # Mirrors get_commission's plain-UTC window convention (ASSUMED for this
    # endpoint — not independently live-verified, see adapter comment).
    httpx_mock.add_response(json={"code": "200000", "data": []})
    adapter = KucoinAdapter(_creds(), make_settings())
    await adapter.get_downline_total(day_start(date(2026, 5, 1)), day_end(date(2026, 5, 1)))
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["rebateStartAt"][0] == "1777593600000"  # 2026-05-01 00:00 UTC (no shift)
