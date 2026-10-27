"""Toobit affiliate adapter tests (mocked HTTP, no live calls)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.base import ReferralCheck
from bot.exchanges.toobit import ToobitAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_DAY = date(2026, 7, 1)


def _creds() -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE["toobit"], SecretStr("api-key"), SecretStr("api-secret"), None
    )


def _page(rows: list[dict], pages: int = 1) -> dict:
    return {
        "code": 200,
        "msg": "success",
        "data": {"pages": str(pages), "total": str(len(rows)), "list": rows},
    }


async def test_toobit_get_commission_sums_usdt(httpx_mock):
    httpx_mock.add_response(
        json=_page(
            [
                {"uid": 111, "commissionVolume": "1.5", "swapCommissionVolume": "1.0"},
                {"uid": 111, "commissionVolume": "2.5"},
            ]
        )
    )
    adapter = ToobitAdapter(_creds(), make_settings())
    result = await adapter.get_commission("111", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert result.total_usdt == Decimal("4.0")
    assert any(ln.asset == "USDT" and ln.amount == Decimal("4.0") for ln in result.lines)
    assert result.settlement_note


async def test_toobit_signs_and_sets_apikey_header(httpx_mock):
    httpx_mock.add_response(json=_page([]))
    adapter = ToobitAdapter(_creds(), make_settings())
    await adapter.get_commission("111", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    req = httpx_mock.get_requests()[0]
    assert req.headers.get("X-BB-APIKEY") == "api-key"
    q = parse_qs(urlparse(str(req.url)).query)
    assert "signature" in q  # Binance-style signed query
    assert q["uid"] == ["111"]
    assert "timestamp" in q


async def test_toobit_downline_total_uses_uid_zero(httpx_mock):
    httpx_mock.add_response(
        json=_page(
            [
                {"uid": 111, "commissionVolume": "10"},
                {"uid": 222, "commissionVolume": "5"},
            ]
        )
    )
    adapter = ToobitAdapter(_creds(), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert total == Decimal("15")  # whole downline, both invitees
    q = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert q["uid"] == ["0"]  # uid=0 disables the per-invitee filter


async def test_toobit_verify_referral_confirmed(httpx_mock):
    httpx_mock.add_response(
        json={"code": 200, "msg": "success", "data": {"uid": 111, "directInvitation": True}}
    )
    adapter = ToobitAdapter(_creds(), make_settings())
    assert await adapter.verify_referral("111") == ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_toobit_verify_referral_not_referral(httpx_mock):
    # -1153 "User not exist" -> authoritative NOT_REFERRAL.
    httpx_mock.add_response(json={"code": -1153, "msg": "User not exist", "data": None})
    adapter = ToobitAdapter(_creds(), make_settings())
    assert await adapter.verify_referral("999") == ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_toobit_get_commission_empty_is_zero(httpx_mock):
    httpx_mock.add_response(json=_page([]))
    adapter = ToobitAdapter(_creds(), make_settings())
    result = await adapter.get_commission("111", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert result.total_usdt == Decimal(0)
    assert result.is_empty
