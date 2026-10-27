"""Adapter tests against mocked HTTP responses (no live API calls)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.base import (
    ExchangeApiError,
    ExchangeAuthError,
    ExchangeRateLimitError,
    ExchangeUnavailableError,
)
from bot.exchanges.binance import BinanceAdapter
from bot.exchanges.bitget import BitgetAdapter
from bot.exchanges.bitmart import BitmartAdapter
from bot.exchanges.bybit import BybitAdapter
from bot.exchanges.gate import GateAdapter
from bot.exchanges.kucoin import KucoinAdapter
from bot.exchanges.mexc import MexcAdapter
from bot.exchanges.okx import OkxAdapter
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


# ----------------------------------------------------------------- WEEX -1054
# One mocked response serves every matching request (get_commission hits SPOT + FUTURES,
# and retries hit the same endpoint repeatedly).
@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_weex_uid_not_in_channel_returns_empty(httpx_mock):
    # VERIFIED (live): a uid not under the partner's channel makes WEEX answer the
    # commission endpoint with business code -1054, delivered — unusually — as HTTP 500.
    # get_commission must treat that as 0 commission for the uid (empty result), NOT as a
    # transient outage: the shared _send would otherwise retry the 500 and bury the code
    # as ExchangeUnavailableError (a false "WEEX down" every accrual cycle, forever).
    httpx_mock.add_response(
        status_code=500,
        json={"code": -1054, "msg": "Query users whose UID is not under the current channel."},
    )
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    result = await adapter.get_commission("1", day_start(_DAY), day_end(_DAY))
    assert result.is_empty
    assert result.raw_records_count == 0


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_weex_sums_spot_and_futures(httpx_mock):
    # A real uid returns records under `channelCommissionInfoItems`; get_commission queries
    # both SPOT and FUTURES and sums, so the same 1.5 USDT record counts twice -> 3.0.
    httpx_mock.add_response(
        json={
            "code": "00000",
            "channelCommissionInfoItems": [{"coin": "USDT", "commission": "1.5"}],
            "pages": 1,
        },
    )
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    result = await adapter.get_commission("8357587701", day_start(_DAY), day_end(_DAY))
    assert not result.is_empty
    total = sum((line.amount for line in result.lines if line.asset == "USDT"), Decimal(0))
    assert total == Decimal("3.0")


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_weex_genuine_5xx_still_unavailable(httpx_mock):
    # A real server error (no -1054 body) must remain a transient outage, not be swallowed.
    httpx_mock.add_response(status_code=503, json={"code": -9999, "msg": "service unavailable"})
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    with pytest.raises(ExchangeUnavailableError):
        await adapter.get_commission("1", day_start(_DAY), day_end(_DAY))


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


_DAY_NOON_MS = 1777636800000  # 2026-05-01 12:00:00 UTC — inside the _DAY window


_EMPTY_KC = {"code": "200000", "data": {"items": []}}


async def test_kucoin_parses_items_envelope(httpx_mock):
    # The wide month fetch spans two <=30-day chunks -> two requests; the record sits
    # in the first, the second is empty (no double-count).
    httpx_mock.add_response(
        json={
            "code": "200000",
            "data": {
                "items": [{"commission": "10", "currency": "USDT", "tradeTime": _DAY_NOON_MS}]
            },
        }
    )
    httpx_mock.add_response(json=_EMPTY_KC)
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("10")}


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_kucoin_filters_by_uid_via_transaction_endpoint(httpx_mock):
    # Regression: get_commission MUST hit queryTransactionByUid (per-invitee), NOT
    # queryMyCommission (affiliate aggregate that ignores userId and over-credits every
    # referral the whole rebate). The uid must be sent as the `uid` filter.
    httpx_mock.add_response(json={"code": "200000", "data": {"items": []}})
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    await adapter.get_commission("553515896", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    url = urlparse(str(httpx_mock.get_requests()[0].url))
    assert url.path == "/api/v2/affiliate/queryTransactionByUid"
    qs = parse_qs(url.query)
    assert qs["uid"][0] == "553515896"  # per-invitee filter actually sent


async def test_kucoin_user_not_found_treated_as_empty(httpx_mock):
    # 300000 "user not found" is deterministic per uid (a uid that isn't a KuCoin
    # account); it means 0 commission, so get_commission returns empty rather than
    # crashing the accrual cycle. Real invitees never return 300000 (verified live).
    httpx_mock.add_response(status_code=400, json={"msg": "user not found", "code": "300000"})
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    result = await adapter.get_commission("111111111", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert result.lines == []
    assert result.is_empty


async def test_kucoin_widens_fetch_and_filters_by_tradetime(httpx_mock):
    # KuCoin silently drops rows for a narrow window even when trades exist, so
    # get_commission FETCHES a WIDE month window (starting well before the requested
    # day) and then keeps ONLY trades whose tradeTime falls inside the requested day.
    # A trade dated outside the day is excluded. The month spans two <=30-day chunks.
    httpx_mock.add_response(
        json={
            "code": "200000",
            "data": {
                "items": [
                    {"commission": "10", "currency": "USDT", "tradeTime": _DAY_NOON_MS},
                    {"commission": "99", "currency": "USDT", "tradeTime": 1777680000000},  # 05-02
                ]
            },
        }
    )
    httpx_mock.add_response(json=_EMPTY_KC)
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    result = await adapter.get_commission("1", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    # only the in-window trade is counted (the 05-02 one is filtered out by tradeTime)
    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("10")}
    # the FETCH window is WIDE: tradeStartAt is well before the requested day's midnight.
    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert int(qs["tradeStartAt"][0]) < 1777593600000  # < 2026-05-01 00:00 UTC


async def test_binance_sums_income_by_asset(httpx_mock):
    # apiReferral/rebate/recentRecord returns a flat array of per-trade records;
    # get_commission sums `income` per `asset` (any coin), filtered to the customerId.
    httpx_mock.add_response(
        json=[
            {"customerId": "123", "income": "1.5", "asset": "USDT", "time": _DAY_NOON_MS},
            {"customerId": "123", "income": "2.5", "asset": "USDT", "time": _DAY_NOON_MS},
            {"customerId": "123", "income": "0.01", "asset": "BNB", "time": _DAY_NOON_MS},
        ]
    )
    adapter = BinanceAdapter(_creds("binance"), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    amounts = {line.asset: line.amount for line in result.lines}
    assert amounts["USDT"] == Decimal("4.0")
    assert amounts["BNB"] == Decimal("0.01")
    assert result.raw_records_count == 3


async def test_binance_signs_request_and_filters_customer(httpx_mock):
    httpx_mock.add_response(json=[])
    adapter = BinanceAdapter(_creds("binance"), make_settings())
    await adapter.get_commission("553515896", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    req = httpx_mock.get_requests()[0]
    assert req.headers["X-MBX-APIKEY"] == "api-key"  # key in header
    url = urlparse(str(req.url))
    assert url.path == "/sapi/v1/apiReferral/rebate/recentRecord"
    qs = parse_qs(url.query)
    assert qs["customerId"][0] == "553515896"  # per-invitee filter
    assert qs["signature"][0]  # HMAC signature appended
    assert qs["timestamp"][0]


async def test_binance_business_error_raises(httpx_mock):
    # A Binance error envelope (e.g. -2015 no apiReferral permission) must surface,
    # never be silently counted as 0.
    httpx_mock.add_response(
        json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
    )
    adapter = BinanceAdapter(_creds("binance"), make_settings())
    with pytest.raises(ExchangeApiError):
        await adapter.get_commission("1", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_binance_slices_into_7_day_windows(httpx_mock):
    # The endpoint requires a span <= 7 days, so a 20-day period is sliced into 3
    # windows (7 + 7 + 6), each with startTime/endTime <= 7 days apart.
    httpx_mock.add_response(json=[])
    adapter = BinanceAdapter(_creds("binance"), make_settings())
    await adapter.get_commission("1", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 20)))
    await adapter.aclose()

    reqs = httpx_mock.get_requests()
    assert len(reqs) == 3
    for req in reqs:
        qs = parse_qs(urlparse(str(req.url)).query)
        span_ms = int(qs["endTime"][0]) - int(qs["startTime"][0])
        assert span_ms <= 7 * 24 * 3600 * 1000


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_kucoin_skips_records_without_tradetime(httpx_mock):
    # A record lacking a usable tradeTime is skipped, never counted — it can't be
    # attributed to a day, and the wide fetch overlaps many per-day queries.
    httpx_mock.add_response(
        json={
            "code": "200000",
            "data": {"items": [{"commission": "5", "currency": "USDT"}]},  # no tradeTime
        }
    )
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    result = await adapter.get_commission("1", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()
    assert result.is_empty


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, can_send_already_matched_responses=True
)
async def test_kucoin_caches_month_fetch_across_days(httpx_mock):
    # Two per-day calls in the SAME month for the SAME uid must share one wide fetch
    # (cache) — the accrual worker's per-day loop must not re-hit KuCoin for every day
    # (that hammered the endpoint into HTTP 429 and failed the whole user's cycle).
    httpx_mock.add_response(json=_EMPTY_KC)
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    await adapter.get_commission("1", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 1)))
    after_first = len(httpx_mock.get_requests())
    await adapter.get_commission("1", day_start(date(2026, 5, 2)), day_end(date(2026, 5, 2)))
    after_second = len(httpx_mock.get_requests())
    await adapter.aclose()

    assert after_first == 2  # month window = two <=30-day chunks
    assert after_second == 2  # second day served from cache — no new API calls


async def test_bybit_date_range_sums_usdt_for_uid(httpx_mock):
    # aff-user-list returns the target uid on the first page; commissionsVol holds
    # the commission for exactly [startDate, endDate]. Only USDT is reported.
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [
                    {"userId": "111", "commissionsVol": {"USDT": "1.0"}},
                    {
                        "userId": "553515896",
                        "commissionsVol": {"BTC": "0", "MNT": "0", "USDT": "0.83421325"},
                    },
                ],
                "nextPageCursor": "",
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission(
        "553515896", day_start(date(2026, 6, 1)), day_end(date(2026, 6, 30))
    )
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("0.83421325")}
    assert adapter.supports_date_range is True
    # The requested period must be preserved verbatim (no rolling-window rewrite).
    assert result.date_from.date() == date(2026, 6, 1)
    assert result.date_to.date() == date(2026, 6, 30)
    assert not any("скольз" in note for note in result.notes)
    # startDate/endDate sent as YYYY-MM-DD calendar dates.
    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["startDate"][0] == "2026-06-01"
    assert qs["endDate"][0] == "2026-06-30"


async def test_bybit_paginates_to_find_uid(httpx_mock):
    # Target uid is only on the second page; adapter must follow nextPageCursor.
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [{"userId": "111", "commissionsVol": {"USDT": "1.0"}}],
                "nextPageCursor": "CURSOR2",
            },
        }
    )
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [{"userId": "553515896", "commissionsVol": {"USDT": "7.5"}}],
                "nextPageCursor": "",
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission(
        "553515896", day_start(date(2026, 6, 1)), day_end(date(2026, 6, 30))
    )
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("7.5")}
    assert len(httpx_mock.get_requests()) == 2
    qs2 = parse_qs(urlparse(str(httpx_mock.get_requests()[1].url)).query)
    assert qs2["cursor"][0] == "CURSOR2"


async def test_bybit_uid_not_found_returns_empty_with_note(httpx_mock):
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [{"userId": "111", "commissionsVol": {"USDT": "1.0"}}],
                "nextPageCursor": "",
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission(
        "553515896", day_start(date(2026, 6, 1)), day_end(date(2026, 6, 30))
    )
    await adapter.aclose()

    assert result.lines == []
    assert result.is_empty
    assert any("не найден" in note for note in result.notes)


async def test_bybit_shows_only_usdt_and_mnt(httpx_mock):
    # commissionsVol drips in several coins; only USDT/MNT must be reported, with a
    # note about the ignored coin (USDC here).
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "userId": "123",
                        "commissionsVol": {"USDT": "12.5", "MNT": "1000", "USDC": "3"},
                    }
                ],
                "nextPageCursor": "",
            },
        }
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    result = await adapter.get_commission(
        "123", day_start(date(2026, 6, 1)), day_end(date(2026, 6, 30))
    )
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {
        "USDT": Decimal("12.5"),
        "MNT": Decimal("1000"),
    }
    assert any("USDT и MNT" in note for note in result.notes)


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


async def test_mexc_empty_when_uid_absent(httpx_mock):
    # Server filters by uid; if the row isn't ours, nothing is summed.
    httpx_mock.add_response(
        json={"code": 0, "data": {"totalPage": 1, "resultList": [{"uid": "111", "total": "9"}]}}
    )
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    result = await adapter.get_commission("43305891", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert result.is_empty


async def test_mexc_sends_uid_param(httpx_mock):
    httpx_mock.add_response(
        json={
            "code": 0,
            "data": {"totalPage": 1, "resultList": [{"uid": "43305891", "total": "5"}]},
        }
    )
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    await adapter.get_commission("43305891", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["uid"][0] == "43305891"


async def test_mexc_shifts_window_to_utc8(httpx_mock):
    # MEXC buckets by UTC+8 (verified vs cabinet) → query window shifted back 8h.
    httpx_mock.add_response(json={"code": 0, "data": {"totalPage": 1, "resultList": []}})
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    await adapter.get_commission("1", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 1)))
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["startTime"][0] == "1777564800000"  # 01.05 00:00 UTC+8


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
            "data": {
                "endId": "CURSOR2",
                "commissionList": [{"coin": "USDT", "rebateAmount": "0.25"}],
            },
        }
    )
    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    # 100 * 0.5 + 0.25 == 50.25 (cumulative 999 fields ignored).
    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("50.25")}
    assert result.raw_records_count == 101
    assert len(httpx_mock.get_requests()) == 2  # short second page stops pagination


async def test_okx_date_range_sums_usdt_for_uid(httpx_mock):
    # invitee/list?periodType=custom returns totalCommission for exactly [begin, end].
    httpx_mock.add_response(
        json={
            "code": "0",
            "data": [
                {"uid": "999", "totalCommission": "1.0"},
                {"uid": "174919725745315840", "totalCommission": "1.1969061"},
            ],
        }
    )
    adapter = OkxAdapter(_creds("okx", passphrase=True), make_settings())
    result = await adapter.get_commission(
        "174919725745315840", day_start(date(2026, 5, 1)), day_end(date(2026, 6, 29))
    )
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("1.1969061")}
    assert adapter.supports_date_range is True
    assert not any("НАКОПЛЕННУЮ" in note for note in result.notes)
    # One window (< 90 days) -> one request, sent as periodType=custom + begin/end/keyword.
    assert len(httpx_mock.get_requests()) == 1
    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["periodType"][0] == "custom"
    assert qs["keyword"][0] == "174919725745315840"
    assert qs["begin"][0] == "1777593600000"  # 2026-05-01 00:00:00 UTC
    assert "end" in qs


async def test_okx_slices_windows_over_90_days(httpx_mock):
    # A 120-day period must be split into <=90-day windows and summed across them.
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "123", "totalCommission": "10.0"}]})
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "123", "totalCommission": "2.5"}]})
    adapter = OkxAdapter(_creds("okx", passphrase=True), make_settings())
    result = await adapter.get_commission(
        "123", day_start(date(2026, 3, 15)), day_end(date(2026, 7, 12))
    )
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {"USDT": Decimal("12.5")}
    assert len(httpx_mock.get_requests()) == 2  # 120 days -> 90 + 30


async def test_okx_uid_not_found_returns_empty_with_note(httpx_mock):
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "999", "totalCommission": "1.0"}]})
    adapter = OkxAdapter(_creds("okx", passphrase=True), make_settings())
    result = await adapter.get_commission(
        "123", day_start(date(2026, 6, 1)), day_end(date(2026, 6, 30))
    )
    await adapter.aclose()

    assert result.is_empty
    assert any("не найден" in note for note in result.notes)


async def test_okx_warns_when_range_older_than_180_days(httpx_mock):
    # OKX silently returns 0 for begin older than ~180 days; the adapter must warn.
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "123", "totalCommission": "0.0"}]})
    adapter = OkxAdapter(_creds("okx", passphrase=True), make_settings())
    result = await adapter.get_commission(
        "123", day_start(date(2025, 1, 1)), day_end(date(2025, 1, 20))
    )
    await adapter.aclose()

    assert any("180" in note for note in result.notes)


async def test_bitget_shifts_window_to_utc8(httpx_mock):
    # Bitget buckets by UTC+8; the query window must be shifted back 8h so that
    # "01.05" starts at 00:00 UTC+8 (= 30.04 16:00 UTC = ms 1777564800000).
    httpx_mock.add_response(json={"code": "00000", "data": {"endId": "x", "commissionList": []}})
    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    await adapter.get_commission("1", day_start(date(2026, 5, 1)), day_end(date(2026, 5, 1)))
    await adapter.aclose()

    qs = parse_qs(urlparse(str(httpx_mock.get_requests()[0].url)).query)
    assert qs["startTime"][0] == "1777564800000"  # 01.05 00:00 UTC+8


async def test_bitmart_sums_futures_rebate_by_three_currencies(httpx_mock):
    httpx_mock.add_response(
        json={
            "total": 1,
            "size": 50,
            "page": 1,
            "usdt_rebate_sum": "12.5",
            "rebate_detail_page_data": [
                {
                    "rebate_coin": "USDT",
                    "trade_user_id": 123,
                    "total_rebate_amount": "12.5",
                }
            ],
        }
    )
    httpx_mock.add_response(
        json={
            "total": 1,
            "size": 50,
            "page": 1,
            "btc_rebate_sum": "0.001",
            "rebate_detail_page_data": [
                {
                    "rebate_coin": "BTC",
                    "trade_user_id": 123,
                    "total_rebate_amount": "0.001",
                }
            ],
        }
    )
    httpx_mock.add_response(
        json={
            "total": 0,
            "size": 50,
            "page": 1,
            "eth_rebate_sum": "0",
            "rebate_detail_page_data": [],
        }
    )
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    result = await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    assert {line.asset: line.amount for line in result.lines} == {
        "BTC": Decimal("0.001"),
        "USDT": Decimal("12.5"),
    }
    assert result.raw_records_count == 2
    assert "BTC/USDT/ETH" in (result.settlement_note or "")


async def test_bitmart_keyed_request_uses_api_key_only(httpx_mock):
    for _ in range(3):
        httpx_mock.add_response(json={"total": 0, "rebate_detail_page_data": []})
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    await adapter.get_commission("123", day_start(_DAY), day_end(_DAY))
    await adapter.aclose()

    request = httpx_mock.get_requests()[0]
    qs = parse_qs(urlparse(str(request.url)).query)
    assert request.headers["X-BM-KEY"] == "api-key"
    assert "X-BM-SIGN" not in request.headers
    assert qs["user_id"][0] == "123"
    assert qs["currency"][0] == "USDT"
    assert qs["rebate_start_time"][0] == str(int(day_start(_DAY).timestamp()))


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
    assert set(adapters) == {
        "bitget",
        "bitmart",
        "bybit",
        "gate",
        "kucoin",
        "mexc",
        "okx",
        "weex",
        "toobit",
        "binance",
    }


async def test_stub_adapter_returns_demo_data():
    from bot.exchanges.registry import build_adapters

    adapters = build_adapters(make_settings(DEMO_MODE="true"))
    result = await adapters["gate"].get_commission("123", day_start(_DAY), day_end(_DAY))
    assert not result.is_empty
    assert any("DEMO" in note for note in result.notes)
