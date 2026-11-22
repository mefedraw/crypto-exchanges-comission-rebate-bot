"""Referral Pro ownership, display estimates and hard separation from money."""

from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.base import ExchangeApiError, ExchangeAuthError, ReferralCheck
from bot.exchanges.binance import BinanceAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_DAY = date(2026, 7, 10)
_PROFILE = {"regTime": 1700000000000, "referralCode": "EXAMPLE"}


@pytest.fixture
async def adapter():
    creds = ExchangeCredentials(
        SPEC_BY_CODE["binance"], SecretStr("key"), SecretStr("secret"), None
    )
    client = BinanceAdapter(creds, make_settings(http_max_retries=0))
    try:
        yield client
    finally:
        await client.aclose()


def envelope(**fields):
    return {"code": "000000000", "status": "OK", "type": "GENERAL", "data": {**_PROFILE, **fields}}


async def test_signed_period_estimate_is_not_usdt(adapter, httpx_mock):
    httpx_mock.add_response(json=envelope(commission="12.345678901234567890"))
    assert await adapter.get_reported_usd("123", _DAY, _DAY) == Decimal("12.345678901234567890")
    (request,) = httpx_mock.get_requests()
    assert request.method == "GET"
    assert str(request.url).startswith(
        "https://api.binance.com/sapi/v1/affiliate/performance/invitee?"
    )
    assert request.headers["X-MBX-APIKEY"] == "key"
    assert request.url.params["userId"] == "123"
    assert request.url.params["startDate"] == request.url.params["endDate"] == _DAY.isoformat()
    assert "customerId" not in request.url.params
    query, signature = request.url.query.rsplit(b"&signature=", 1)
    assert hmac.digest(b"secret", query, hashlib.sha256).hex().encode() == signature


async def test_zero_reported_only_when_explicit(adapter, httpx_mock):
    httpx_mock.add_response(json=envelope(commission="0"))
    assert await adapter.get_reported_usd("123", _DAY, _DAY) == 0


async def test_decimal_json_number_is_not_converted_through_float(adapter, httpx_mock):
    httpx_mock.add_response(
        content=(
            b'{"code":"000000000","status":"OK","data":{"regTime":1700000000000,'
            b'"referralCode":"EXAMPLE","commission":12.3456789012345678901234567}}'
        )
    )
    assert await adapter.get_reported_usd("123", _DAY, _DAY) == Decimal(
        "12.3456789012345678901234567"
    )


async def test_retry_signs_fresh_after_a_transient_failure(adapter, httpx_mock, monkeypatch):
    monkeypatch.setattr(adapter, "_max_retries", 1)
    monkeypatch.setattr(adapter, "_backoff", AsyncMock())
    monkeypatch.setattr(adapter, "_pace_request", AsyncMock())
    timestamps = iter([1700000000000, 1700000000800])
    monkeypatch.setattr("bot.exchanges.binance.time_ns", lambda: next(timestamps) * 1_000_000)
    httpx_mock.add_response(status_code=503)
    httpx_mock.add_response(json=envelope(commission="1.25"))
    assert await adapter.get_reported_usd("123", _DAY, _DAY) == Decimal("1.25")
    first, second = httpx_mock.get_requests()
    assert first.url.params["timestamp"] == "1700000000000"
    assert second.url.params["timestamp"] == "1700000000800"
    assert first.url.params["signature"] != second.url.params["signature"]


async def test_membership_does_not_require_commission(adapter, httpx_mock):
    httpx_mock.add_response(json=envelope())
    assert await adapter.verify_referral("123") == ReferralCheck.CONFIRMED
    query = httpx_mock.get_requests()[0].url.params
    assert "startDate" not in query and "endDate" not in query


async def test_explicit_non_member(adapter, httpx_mock):
    httpx_mock.add_response(status_code=400, json={"code": -29002, "msg": "not this affiliate"})
    assert await adapter.verify_referral("123") == ReferralCheck.NOT_REFERRAL


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"code": "000000000", "status": "OK", "data": {}},
        {**envelope(commission="10"), "code": "-29002"},
        {**envelope(commission="10"), "status": "ERROR"},
        envelope(commission="1", uid="999"),
        envelope(commission="1", referralCode=""),
        envelope(commission="1", regTime=None),
        envelope(),
        envelope(commission=None),
        envelope(commission=False),
        envelope(commission="-5"),
        envelope(commission="NaN"),
        envelope(commission="Infinity"),
        [{"customerId": "123", "asset": "USDT", "income": "5"}],
    ],
)
async def test_incomplete_responses_never_become_zero(adapter, httpx_mock, payload):
    httpx_mock.add_response(json=payload)
    with pytest.raises(ExchangeApiError):
        await adapter.get_reported_usd("123", _DAY, _DAY)


@pytest.mark.parametrize(
    ("http", "code", "error"),
    [
        (401, -2015, ExchangeAuthError),
        (400, -2015, ExchangeAuthError),
        (400, -1000, ExchangeApiError),
        (500, -29002, ExchangeApiError),
    ],
)
async def test_other_errors_never_reject_membership_or_leak_body(
    adapter, httpx_mock, http, code, error
):
    httpx_mock.add_response(status_code=http, json={"code": code, "msg": "sensitive-test-marker"})
    with pytest.raises(error) as caught:
        await adapter.verify_referral("123")
    assert "sensitive-test-marker" not in str(caught.value)


async def test_dated_invitee_only_fixed_rate_no_downline(adapter, httpx_mock):
    httpx_mock.add_response(json=envelope(commission="2.34567"))
    httpx_mock.add_response(json=envelope(commission="3.00003"))
    end = _DAY + timedelta(days=30)
    result = await adapter.get_commission("123", day_start(_DAY), day_end(end))
    assert [(line.asset, line.amount) for line in result.lines] == [("USDT", Decimal("5.34570"))]
    assert result.total_usdt is None and "1 USD = 1 USDT" in result.settlement_note
    assert [
        (r.url.params["startDate"], r.url.params["endDate"]) for r in httpx_mock.get_requests()
    ] == [
        (_DAY.isoformat(), (end - timedelta(days=1)).isoformat()),
        (end.isoformat(), end.isoformat()),
    ]
    assert await adapter.get_downline_total(day_start(_DAY), day_end(_DAY)) is None


@pytest.mark.parametrize("status,payload", [(200, envelope()), (400, {"code": -29002})])
async def test_no_payable_profile_is_retried_next_cycle(adapter, httpx_mock, status, payload):
    httpx_mock.add_response(status_code=status, json=payload)
    assert await adapter.accrual_start("123", _DAY) is None


async def test_volume_without_commission_is_not_zero(adapter, httpx_mock):
    httpx_mock.add_response(json=envelope(tradeVol="125"))
    with pytest.raises(ExchangeApiError, match="without commission"):
        await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))


@pytest.mark.parametrize("matches", [True, False])
async def test_omitted_day_requires_unchanged_commission_and_volume(
    adapter, httpx_mock, monkeypatch, matches
):
    monkeypatch.setattr(adapter, "_pace_request", AsyncMock())
    totals = {"commission": "1.2", "tradeVol": "500"}
    httpx_mock.add_response(json=envelope())
    for _ in range(1 if matches else 2):
        httpx_mock.add_response(json=envelope(**totals))
        httpx_mock.add_response(
            json=envelope(commission="1.2", tradeVol="500" if matches else "501")
        )
    if matches:
        result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
        assert result.lines[0].amount == 0 and result.raw_records_count == 0
    else:
        with pytest.raises(ExchangeApiError, match="no independent confirmation"):
            await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))


async def test_reject_invalid_input_before_signing(adapter, httpx_mock):
    with pytest.raises(ExchangeApiError):
        await adapter.verify_referral("123&userId=999")
    with pytest.raises(ValueError):
        await adapter.get_reported_usd("123", date(2026, 7, 11), _DAY)
    assert httpx_mock.get_requests() == []
