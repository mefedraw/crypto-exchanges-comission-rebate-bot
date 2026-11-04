from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.base import ExchangeApiError
from bot.exchanges.weex import WeexAdapter, _millis, _query_windows
from tests.conftest import make_settings

TZ = timezone(timedelta(hours=8))


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(WeexAdapter, "min_request_interval_seconds", 0)
    return WeexAdapter(
        ExchangeCredentials(
            SPEC_BY_CODE["weex"], SecretStr("key"), SecretStr("secret"), SecretStr("pass")
        ),
        make_settings(HTTP_MAX_RETRIES=0),
    )


def test_exact_utc8_month_splits_without_overlap_or_lost_milliseconds():
    start = datetime(2026, 10, 1, tzinfo=TZ)
    end = datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=TZ)
    windows = list(_query_windows(start, end, 30))
    assert len(windows) == 2
    assert windows[0][0] == datetime(2026, 9, 30, 16, tzinfo=UTC)
    assert windows[0][1] + timedelta(milliseconds=1) == windows[1][0]
    assert windows[-1][1] == datetime(2026, 10, 31, 15, 59, 59, 999000, tzinfo=UTC)
    assert _millis(windows[-1][1]) == 1793462399999


def test_query_bounds_require_aware_ordered_dates():
    with pytest.raises(ValueError):
        list(_query_windows(datetime(2026, 10, 1), datetime(2026, 10, 2), 30))
    with pytest.raises(ValueError):
        list(
            _query_windows(datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC), 30)
        )


async def test_commission_uses_exact_instants_including_clipped_legacy_tail(adapter, httpx_mock):
    for _ in range(2):
        httpx_mock.add_response(json={"channelCommissionInfoItems": [], "pages": 0, "total": 0})
    start = datetime(2026, 10, 7, 0, tzinfo=UTC)
    end = datetime(2026, 10, 7, 15, 59, 59, 999000, tzinfo=UTC)
    await adapter.get_commission("123", start, end)
    for request in httpx_mock.get_requests():
        qs = parse_qs(urlparse(str(request.url)).query)
        assert int(qs["startTime"][0]) == _millis(start)
        assert int(qs["endTime"][0]) == _millis(end)
    await adapter.aclose()


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
async def test_discovery_short_pages_are_not_truncated_or_summed(adapter, httpx_mock):
    requests = []

    def respond(request):
        qs = request.url.params
        requests.append(str(request.url))
        if "uid" in qs:
            rows = [{"coin": "USDT", "commission": "0.123456789012345678"}]
            return httpx.Response(
                200, json={"channelCommissionInfoItems": rows, "pages": 1, "total": 1}
            )
        if qs["productType"] == "SPOT":
            return httpx.Response(
                200, json={"channelCommissionInfoItems": [], "pages": 0, "total": 0}
            )
        # One record per page even though the requested page size is 100.
        uid = "111" if qs["page"] == "1" else "222"
        return httpx.Response(
            200,
            json={
                "channelCommissionInfoItems": [{"uid": uid, "commission": "999999"}],
                "pages": 2,
                "total": 2,
            },
        )

    httpx_mock.add_callback(respond)
    result = await adapter.get_downline_total(
        datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 10, 2, tzinfo=TZ)
    )
    assert result == Decimal("0.493827156049382712")
    assert any("page=2" in url for url in requests)
    assert all("getAffiliateUIDs" not in url for url in requests)
    await adapter.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "00000"},
        {"channelCommissionInfoItems": [{"commission": "1"}], "pages": 1},
        {"channelCommissionInfoItems": [], "pages": 2, "total": 2},
        {"channelCommissionInfoItems": [], "pages": "bad"},
        {"code": "-1054"},
    ],
)
async def test_discovery_failure_never_becomes_zero(adapter, httpx_mock, payload):
    httpx_mock.add_response(json=payload)
    with pytest.raises(ExchangeApiError):
        await adapter.get_downline_total(
            datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 10, 2, tzinfo=TZ)
        )
    await adapter.aclose()


async def test_later_discovery_failure_does_not_return_partial_total(adapter, httpx_mock):
    httpx_mock.add_response(
        json={
            "channelCommissionInfoItems": [{"uid": "123", "commission": "1"}],
            "pages": 2,
            "total": 2,
        }
    )
    httpx_mock.add_response(status_code=500)
    with pytest.raises(ExchangeApiError):
        await adapter.get_downline_total(
            datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 10, 2, tzinfo=TZ)
        )
    await adapter.aclose()


async def test_filtered_response_cannot_credit_another_uid(adapter, httpx_mock):
    httpx_mock.add_response(
        json={
            "channelCommissionInfoItems": [{"uid": "999", "coin": "USDT", "commission": "10"}],
            "pages": 1,
            "total": 1,
        }
    )
    with pytest.raises(ExchangeApiError, match="UID mismatch"):
        await adapter.get_commission(
            "123", datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 10, 2, tzinfo=TZ)
        )
    await adapter.aclose()
