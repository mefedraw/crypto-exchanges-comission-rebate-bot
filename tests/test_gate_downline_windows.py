from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.gate import GateAdapter
from tests.conftest import make_settings


@pytest.mark.parametrize("days,windows", [(1, 1), (31, 2)])
async def test_downline_preserves_payout_timezone_and_window_bounds(httpx_mock, days, windows):
    for _ in range(windows):
        httpx_mock.add_response(
            json={
                "list": [
                    {"commission_asset": "USDT", "commission_amount": "1.25"},
                    {"commission_asset": "BTC", "commission_amount": "0.01"},
                ]
            }
        )
    credentials = ExchangeCredentials(
        SPEC_BY_CODE["gate"], SecretStr("api-key"), SecretStr("api-secret")
    )
    adapter = GateAdapter(credentials, make_settings())
    start = datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))
    end = start + timedelta(days=days, microseconds=-1)
    try:
        total = await adapter.get_downline_total(start, end)
    finally:
        await adapter.aclose()

    queries = [parse_qs(request.url.query.decode()) for request in httpx_mock.get_requests()]
    assert len(queries) == windows
    assert int(queries[0]["from"][0]) == int(datetime(2026, 8, 31, 16, tzinfo=UTC).timestamp())
    assert int(queries[-1]["to"][0]) == int(end.timestamp())
    assert total == Decimal("1.25") * windows
    assert all("user_id" not in query for query in queries)
