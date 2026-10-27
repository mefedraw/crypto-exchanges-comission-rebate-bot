"""Tests for WeexAdapter.get_downline_total — list-then-sum.

VERIFIED (live 2026-07-20): ``getAffiliateUIDs`` without a uid filter enumerates the
whole channel (rows under ``channelUserInfoItemList``, paginated via ``pages``). The
adapter lists every uid, then sums each one's spot+futures USDT commission.
"""

from __future__ import annotations

import re
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
    # getAffiliateUIDs (no uid) enumerates the channel: 2 uids on one page.
    httpx_mock.add_response(
        url=re.compile(r".*getAffiliateUIDs.*"),
        json={
            "channelUserInfoItemList": [{"uid": "111"}, {"uid": "222"}],
            "pages": 1,
            "total": 2,
            "pageSize": 100,
        },
    )
    # Each uid's commission call (SPOT + FUTURES) returns the same 1.5 USDT record.
    httpx_mock.add_response(
        url=re.compile(r".*getAffiliateCommission.*"),
        json={
            "code": "00000",
            "channelCommissionInfoItems": [{"coin": "USDT", "commission": "1.5"}],
            "pages": 1,
        },
    )
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    # 2 uids × (SPOT 1.5 + FUTURES 1.5) = 2 × 3.0 = 6.0
    assert total == Decimal("6.0")


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_weex_downline_total_empty_channel_is_zero(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r".*getAffiliateUIDs.*"),
        json={"channelUserInfoItemList": [], "pages": 1, "total": 0, "pageSize": 100},
    )
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert total == Decimal(0)  # empty channel -> 0, never None (endpoint works)
