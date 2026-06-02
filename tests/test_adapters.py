"""Adapter tests against mocked HTTP responses (no live API calls)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.base import ExchangeAuthError, ExchangeRateLimitError
from bot.exchanges.bitget import BitgetAdapter
from bot.exchanges.bybit import BybitAdapter
from bot.exchanges.gate import GateAdapter
from bot.exchanges.kucoin import KucoinAdapter
from bot.exchanges.okx import OkxAdapter
from bot.exchanges.mexc import MexcAdapter
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


async def test_gate_sums_by_asset(httpx_mock):
    httpx_mock.add_response(
        json=[
            {"commission_amount": "1.5", "commission_asset": "USDT"},
            {"commission_amount": "2.5", "commission_asset": "USDT"},
            {"commission_amount": "0.001", "commission_asset": "BTC"},
        ]
    )
    adapter = GateAdapter(_creds("gate"), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    amounts = {line.asset: line.amount for line in result.lines}
    assert amounts["USDT"] == Decimal("4.0")
    assert amounts["BTC"] == Decimal("0.001")
    assert result.raw_records_count == 3
    assert result.settlement_note  # disclaimer text present


async def test_gate_separates_by_source(httpx_mock):
    # Live shape: same asset, different source -> reported as separate lines.
    httpx_mock.add_response(
        json=[
            {"commission_amount": "1.0", "commission_asset": "USDT", "source": "FUTURES"},
            {"commission_amount": "2.0", "commission_asset": "USDT", "source": "FUTURES"},
            {"commission_amount": "0.5", "commission_asset": "USDT", "source": "SPOT"},
        ]
    )
    adapter = GateAdapter(_creds("gate"), make_settings())
    result = await adapter.get_commission("48976844", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    by_source = {(l.asset, l.source): l.amount for l in result.lines}
    assert by_source[("USDT", "FUTURES")] == Decimal("3.0")
    assert by_source[("USDT", "SPOT")] == Decimal("0.5")


async def test_gate_signs_request(httpx_mock):
    httpx_mock.add_response(json=[])
    adapter = GateAdapter(_creds("gate"), make_settings())
    await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    request = httpx_mock.get_requests()[0]
    assert request.headers["KEY"] == "api-key"
    assert request.headers["SIGN"]  # signature attached
    assert "Timestamp" in request.headers


async def test_kucoin_parses_items_envelope(httpx_mock):
    httpx_mock.add_response(
        json={"code": "200000", "data": {"items": [{"commission": "10", "currency": "USDT"}]}}
    )
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("10")}


async def test_kucoin_shifts_window_to_utc8(httpx_mock):
    # KuCoin buckets rebate periods by UTC+8 → query window shifted back 8h.
    httpx_mock.add_response(json={"code": "200000", "data": {"items": []}})
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    await adapter.get_commission("1", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 1)))
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["rebateStartAt"][0] == "1777564800000"  # 01.05 00:00 UTC+8


async def test_bybit_uses_rolling_window_and_warns(httpx_mock):
    httpx_mock.add_response(
        json={"retCode": 0, "result": {"commissions30Day": {"USDT": "12.5"}}}
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("12.5")}
    assert adapter.supports_date_range is False
    assert any("скольз" in note for note in result.notes)
    assert len(httpx_mock.get_requests()) == 1  # no per-window paging for Bybit


async def test_bybit_shows_only_usdt(httpx_mock):
    # Commission drips in several coins; only USDT must be reported.
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {"commissions30Day": {"USDT": "12.5", "MNT": "1000", "USDC": "3"}},
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert {line.asset for line in result.lines} == {"USDT"}
    assert result.lines[0].amount == Decimal("12.5")
    assert any("только USDT" in note for note in result.notes)


async def test_bybit_real_shape_month_picks_30day(httpx_mock):
    # Live-shaped response: per-coin map with empty strings for coins w/o commission.
    # A 31-day (May) query must map to the rolling 30-day window, not 365-day.
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "commissions30Day": {"BTC": "", "ETH": "", "MNT": "", "USDC": "", "USDT": "148.64458062"},
                "commissions365Day": {"BTC": "", "ETH": "", "MNT": "", "USDC": "", "USDT": "446.47517676"},
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission(
        "542030785", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 31))
    )
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("148.64458062")}
    # Empty-string coins must not trigger the "other coins" note.
    assert not any("только USDT" in note for note in result.notes)
    # Period must reflect the rolling 30-day window (ending today), not the request.
    today = datetime.now(timezone.utc).date()
    assert result.date_to.date() == today
    assert (result.date_to.date() - result.date_from.date()).days == 30


async def test_bybit_list_shape_only_usdt(httpx_mock):
    # Same requirement when the breakdown is a list of {coin, amount} rows.
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "commissions30Day": [
                    {"coin": "MNT", "amount": "1000"},
                    {"coin": "USDT", "amount": "7.25"},
                ]
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("7.25")}


async def test_mexc_filters_uid_and_paginates(httpx_mock):
    # Live-shaped response: rows under data.resultList, pagination via totalPage,
    # uid in `uid`, amount in `total`. Target uid spans two pages -> summed.
    httpx_mock.add_response(
        json={
            "code": 0,
            "data": {
                "totalPage": 2,
                "resultList": [
                    {"uid": "43305891", "total": "5.0", "spot": "0", "futures": "5.0"},
                    {"uid": "99999999", "total": "3.0"},
                ],
            },
        }
    )
    httpx_mock.add_response(
        json={
            "code": 0,
            "data": {
                "totalPage": 2,
                "resultList": [{"uid": "43305891", "total": "2.5"}],
            },
        }
    )
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    result = await adapter.get_commission("43305891", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("7.5")}
    assert result.total_usdt == Decimal("7.5")
    assert result.raw_records_count == 3  # all scanned invitee rows
    assert len(httpx_mock.get_requests()) == 2  # stopped at totalPage


async def test_mexc_unmatched_uid_adds_note(httpx_mock):
    httpx_mock.add_response(
        json={"code": 0, "data": {"totalPage": 1, "resultList": [{"uid": "111", "total": "9"}]}}
    )
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    result = await adapter.get_commission("43305891", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert result.is_empty
    assert any("uid-фильтр" in note for note in result.notes)


async def test_bitget_paginates_via_endid_and_ignores_cumulative(httpx_mock):
    # Live shape: records under data.commissionList; paginate via response endId;
    # sum per-record `rebateAmount`, NOT the cumulative totalRebateAmount.
    full_page = [
        {"coin": "USDT", "rebateAmount": "0.5", "totalRebateAmount": "999"} for _ in range(100)
    ]
    httpx_mock.add_response(
        json={"code": "00000", "data": {"endId": "CURSOR1", "commissionList": full_page}}
    )
    httpx_mock.add_response(
        json={
            "code": "00000",
            "data": {"endId": "CURSOR2", "commissionList": [{"coin": "USDT", "rebateAmount": "0.25"}]},
        }
    )
    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    # 100 * 0.5 + 0.25 == 50.25 (cumulative 999 fields ignored).
    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("50.25")}
    assert result.raw_records_count == 101
    assert len(httpx_mock.get_requests()) == 2  # short second page stops pagination


async def test_okx_cumulative_totalcommission_with_warning(httpx_mock):
    # Live shape: data[0].totalCommission is the cumulative (date-independent) total.
    httpx_mock.add_response(
        json={
            "code": "0",
            "data": [{"totalCommission": "227.02", "accFee": "454.04", "totalVol": "934116.52"}],
        }
    )
    adapter = OkxAdapter(_creds("okx", passphrase=True), make_settings())
    result = await adapter.get_commission(
        "174919725745315840", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 31))
    )
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("227.02")}
    assert adapter.supports_date_range is False
    assert any("НАКОПЛЕННУЮ" in note for note in result.notes)


async def test_bitget_shifts_window_to_utc8(httpx_mock):
    # Bitget buckets by UTC+8; the query window must be shifted back 8h so that
    # "01.05" starts at 00:00 UTC+8 (= 30.04 16:00 UTC = ms 1777564800000).
    httpx_mock.add_response(json={"code": "00000", "data": {"endId": "x", "commissionList": []}})
    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    await adapter.get_commission("1", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 1)))
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["startTime"][0] == "1777564800000"  # 01.05 00:00 UTC+8


async def test_auth_error_mapped(httpx_mock):
    httpx_mock.add_response(status_code=401)
    adapter = GateAdapter(_creds("gate"), make_settings(HTTP_MAX_RETRIES="0"))
    with pytest.raises(ExchangeAuthError):
        await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()


async def test_rate_limit_error_after_retries(httpx_mock):
    # 429 on every attempt -> exhausts retries -> ExchangeRateLimitError.
    httpx_mock.add_response(status_code=429)
    adapter = GateAdapter(_creds("gate"), make_settings(HTTP_MAX_RETRIES="0"))
    with pytest.raises(ExchangeRateLimitError):
        await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()


def test_demo_mode_builds_all_stub_adapters():
    from bot.exchanges.registry import build_adapters

    adapters = build_adapters(make_settings(DEMO_MODE="true"))
    assert set(adapters) == {"gate", "kucoin", "mexc", "bitget", "okx", "bybit", "weex"}


async def test_stub_adapter_returns_demo_data():
    from bot.exchanges.registry import build_adapters

    adapters = build_adapters(make_settings(DEMO_MODE="true"))
    result = await adapters["gate"].get_commission("123", day_start(_DAY), day_end(_DAY))
    assert not result.is_empty
    assert any("DEMO" in note for note in result.notes)
