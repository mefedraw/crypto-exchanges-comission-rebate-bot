from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.gate import GateAdapter
from tests.conftest import make_settings

PAYOUT_TZ = timezone(timedelta(hours=8))


def _adapter() -> GateAdapter:
    credentials = ExchangeCredentials(
        SPEC_BY_CODE["gate"], SecretStr("api-key"), SecretStr("api-secret")
    )
    return GateAdapter(credentials, make_settings())


async def test_gate_preserves_payout_day_boundaries(httpx_mock):
    httpx_mock.add_response(json={"list": [], "total": 0})
    adapter = _adapter()
    start = datetime(2026, 10, 17, tzinfo=PAYOUT_TZ)
    end = start + timedelta(days=1, microseconds=-1)
    try:
        await adapter.get_commission("12345678", start, end)
    finally:
        await adapter.aclose()

    query = parse_qs(httpx_mock.get_requests()[0].url.query.decode())
    assert int(query["from"][0]) == int(datetime(2026, 10, 16, 16, tzinfo=UTC).timestamp())
    assert int(query["to"][0]) == int(datetime(2026, 10, 17, 15, 59, 59, tzinfo=UTC).timestamp())
    assert query["user_id"] == ["12345678"]


async def test_gate_preserves_partial_day_and_mixed_timezones(httpx_mock):
    httpx_mock.add_response(json=[])
    adapter = _adapter()
    start = datetime(2026, 10, 17, 14, 37, 12, 500000, tzinfo=UTC)
    end = datetime(2026, 10, 17, 23, 42, 10, 250000, tzinfo=PAYOUT_TZ)
    try:
        await adapter.get_commission("12345678", start, end)
    finally:
        await adapter.aclose()

    query = parse_qs(httpx_mock.get_requests()[0].url.query.decode())
    assert int(query["from"][0]) == int(start.timestamp())
    assert int(query["to"][0]) == int(end.timestamp())


@pytest.mark.parametrize("days,windows", [(1, 1), (30, 1), (31, 2), (60, 2)])
async def test_gate_windows_have_no_gaps_or_overlapping_seconds(httpx_mock, days, windows):
    for _ in range(windows):
        httpx_mock.add_response(json=[])
    start = datetime(2026, 8, 1, 12, 5, 20, 500000, tzinfo=PAYOUT_TZ)
    end = start + timedelta(days=days, seconds=-1)
    adapter = _adapter()
    try:
        await adapter.get_commission("12345678", start, end)
    finally:
        await adapter.aclose()

    queries = [parse_qs(request.url.query.decode()) for request in httpx_mock.get_requests()]
    bounds = [(int(query["from"][0]), int(query["to"][0])) for query in queries]
    assert len(bounds) == windows
    assert bounds[0][0] == int(start.timestamp())
    assert bounds[-1][1] == int(end.timestamp())
    assert all(0 <= last - first < 30 * 86400 for first, last in bounds)
    assert all(bounds[i][0] == bounds[i - 1][1] + 1 for i in range(1, len(bounds)))
    assert all(query["offset"] == ["0"] for query in queries)


async def test_gate_pagination_keeps_the_same_timezone_window(httpx_mock):
    record = {"commission_amount": "0.01", "commission_asset": "USDT", "source": "SPOT"}
    httpx_mock.add_response(json={"list": [record] * 100, "total": 101})
    httpx_mock.add_response(json={"list": [record], "total": 101})
    start = datetime(2026, 10, 17, tzinfo=PAYOUT_TZ)
    end = start + timedelta(days=1, microseconds=-1)
    adapter = _adapter()
    try:
        result = await adapter.get_commission("12345678", start, end)
    finally:
        await adapter.aclose()

    queries = [parse_qs(request.url.query.decode()) for request in httpx_mock.get_requests()]
    assert [query["offset"] for query in queries] == [["0"], ["100"]]
    assert all(int(query["from"][0]) == int(start.timestamp()) for query in queries)
    assert all(int(query["to"][0]) == int(end.timestamp()) for query in queries)
    assert result.raw_records_count == 101
    assert result.lines[0].amount == Decimal("1.01")


async def test_gate_rejects_naive_datetime_before_request(httpx_mock):
    adapter = _adapter()
    try:
        with pytest.raises(ValueError, match="timezone-aware"):
            await adapter.get_commission(
                "12345678", datetime(2026, 10, 17), datetime(2026, 10, 18, tzinfo=UTC)
            )
    finally:
        await adapter.aclose()
    assert not httpx_mock.get_requests()
