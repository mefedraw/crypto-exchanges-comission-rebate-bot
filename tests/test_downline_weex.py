"""Commission-driven UID discovery followed by owner-scoped commission sums."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.weex import WeexAdapter
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


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_weex_downline_total_lists_uids_and_sums(httpx_mock):
    def response(request):
        import httpx

        if "uid" not in request.url.params:
            # Both parent and child income may be present; these amounts are NOT summed.
            rows = [{"uid": "111", "commission": "900"}, {"uid": "222", "commission": "800"}]
        else:
            rows = [{"coin": "USDT", "commission": "1.5"}]
        return httpx.Response(
            200, json={"channelCommissionInfoItems": rows, "pages": 1, "total": len(rows)}
        )

    httpx_mock.add_callback(response)
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    # 2 uids × (SPOT 1.5 + FUTURES 1.5) = 2 × 3.0 = 6.0
    assert total == Decimal("6.0")
    assert all("getAffiliateUIDs" not in str(r.url) for r in httpx_mock.get_requests())


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_weex_downline_total_empty_channel_is_zero(httpx_mock):
    httpx_mock.add_response(
        json={"channelCommissionInfoItems": [], "pages": 0, "total": 0, "pageSize": 100},
    )
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert total == Decimal(0)  # empty channel -> 0, never None (endpoint works)
