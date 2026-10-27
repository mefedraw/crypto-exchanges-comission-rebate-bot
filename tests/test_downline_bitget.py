"""get_downline_total for Bitget: list-then-sum across the whole customer roster.

Bitget's own docs mark `uid` on customer-commissions as "Required: No" but never
say what an omitted uid returns, so (unlike Gate/MEXC/BitMart) the adapter does
NOT rely on that shortcut. Instead it enumerates every customer via
`/api/v2/broker/customer-list` (no uid/time filter, paginated via pageNo/pageSize)
and sums each customer's USDT commission with the same per-uid pagination
`get_commission` already uses.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.bitget import BitgetAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_DAY = date(2026, 5, 1)


def _creds(code: str, *, passphrase: bool = False) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code],
        SecretStr("api-key"),
        SecretStr("api-secret"),
        SecretStr("pass") if passphrase else None,
    )


async def test_bitget_downline_total_sums_usdt_across_all_customers(httpx_mock):
    # 1) customer-list: no uid/time filter -> whole roster, one short page (stops
    #    pagination immediately since len(records) < pageSize).
    httpx_mock.add_response(
        json={
            "code": "00000",
            "data": [
                {"uid": "10", "registerTime": "1"},
                {"uid": "20", "registerTime": "2"},
            ],
        }
    )
    # 2) customer-commissions for uid "10": one USDT row + one non-USDT row (ignored).
    httpx_mock.add_response(
        json={
            "code": "00000",
            "data": {
                "endId": "e1",
                "commissionList": [
                    {"coin": "USDT", "rebateAmount": "5.0"},
                    {"coin": "BTC", "rebateAmount": "0.001"},
                ],
            },
        }
    )
    # 3) customer-commissions for uid "20": two USDT rows.
    httpx_mock.add_response(
        json={
            "code": "00000",
            "data": {
                "endId": "e2",
                "commissionList": [
                    {"coin": "USDT", "rebateAmount": "2.5"},
                    {"coin": "USDT", "rebateAmount": "1.25"},
                ],
            },
        }
    )

    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal("8.75")

    requests = httpx_mock.get_requests()
    assert len(requests) == 3
    roster_request = requests[0]
    assert roster_request.method == "POST"
    assert str(roster_request.url).endswith("/api/v2/broker/customer-list")
    assert roster_request.read() == b'{"pageNo":"1","pageSize":"100"}'

    for request in requests[1:]:
        assert request.method == "GET"
        assert "/api/v2/broker/customer-commissions" in str(request.url)


async def test_bitget_downline_total_zero_when_roster_empty(httpx_mock):
    httpx_mock.add_response(json={"code": "00000", "data": []})
    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal(0)
    assert len(httpx_mock.get_requests()) == 1  # only the (empty) roster call
