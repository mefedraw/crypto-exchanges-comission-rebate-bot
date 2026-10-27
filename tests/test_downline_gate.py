"""Gate whole-downline USDT total: commission_history without a user_id filter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.gate import GateAdapter
from bot.utils.dates import day_end, day_start
from tests.conftest import make_settings

_DAY = date(2026, 5, 1)


def _creds(code: str) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code], SecretStr("api-key"), SecretStr("api-secret"), None
    )


async def test_gate_downline_total_sums_usdt_only(httpx_mock):
    httpx_mock.add_response(
        json=[
            {"commission_amount": "1.5", "commission_asset": "USDT"},
            {"commission_amount": "2.5", "commission_asset": "USDT"},
            {"commission_amount": "0.001", "commission_asset": "BTC"},  # non-USDT ignored
        ]
    )
    adapter = GateAdapter(_creds("gate"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert total == Decimal("4.0")


async def test_gate_downline_total_omits_user_id_filter(httpx_mock):
    httpx_mock.add_response(json=[])
    adapter = GateAdapter(_creds("gate"), make_settings())
    total = await adapter.get_downline_total(day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert total == Decimal(0)  # empty downline -> 0, never None
    request = httpx_mock.get_requests()[0]
    params = parse_qs(urlparse(str(request.url)).query)
    assert "user_id" not in params  # whole downline, not one referral
