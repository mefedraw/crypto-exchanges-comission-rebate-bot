"""Partner document fixtures and regression tests; never call a live exchange."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges import bitbase, blofin
from bot.exchanges.base import ExchangeApiError, ExchangeAuthError, ReferralCheck
from bot.exchanges.bitbase import BitbaseAdapter
from bot.exchanges.bitunix import BitunixAdapter, partner_signature
from bot.exchanges.blofin import BlofinAdapter
from bot.exchanges.partner_http import PartnerHttpAdapter, amount_of
from bot.exchanges.registry import build_adapter, build_adapters
from bot.utils.dates import day_end, day_start, to_millis
from tests.conftest import make_settings

_DAY = date(2026, 7, 1)
_START, _END = day_start(_DAY), day_end(_DAY)
_NOW = datetime(2026, 7, 10, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clock_and_pacing(monkeypatch):
    # Freeze wall time while keeping monotonic time intact. Skip the exchange
    # pacing sleeps in mocks; production still serializes/rate-limits requests.
    monkeypatch.setattr("time.time", lambda: _NOW.timestamp())
    monkeypatch.setattr(PartnerHttpAdapter, "request_interval", 0)
    for cls in (BitunixAdapter, BitbaseAdapter, BlofinAdapter):
        monkeypatch.setattr(cls, "request_interval", 0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW.astimezone(tz) if tz is not None else _NOW.replace(tzinfo=None)

    monkeypatch.setattr(bitbase, "datetime", Clock)


def _creds(code):
    return ExchangeCredentials(
        SPEC_BY_CODE[code], SecretStr("api-key"), SecretStr("api-secret"), SecretStr("pass")
    )


def _adapter(cls, **overrides):
    settings = make_settings(
        **{
            "BLOFIN_COMMISSION_ASSET": "USDT",
            "HTTP_MAX_RETRIES": "0",
            **overrides,
        }
    )
    return cls(_creds(cls.code), settings)


def _bitbase_row(uid="123", spot="0.899762738679", futures="1.3395768975"):
    # VERIFIED: the monetary fields and values are from the supplied PDF, pp. 5-9.
    return {
        "userId": uid,
        "commissionAmountU": str(Decimal(spot) + Decimal(futures)),
        "spotCommissionAmountU": spot,
        "futuresCommissionAmountU": futures,
        "feeU": "4.97631030262",
        "balanceAmountU": "88.5",
    }


def _bitbase_response(result):
    return {"rc": 0, "mc": "SUCCESS", "ma": [], "result": result}


def test_bitunix_signature_document_example():
    params = {
        "name": "bitunix",
        "age": 18,
        "Country": "USA",
        "24hProfit": "0.59",
        "timestamp": 1694173446,
    }
    assert partner_signature(params, "thisistestapisecret") == (
        "d57871af95165be55a43e437a25cd53ce096ab8a"
    )


async def test_bitunix_native_assets_and_partner_perspective(httpx_mock):
    httpx_mock.add_response(
        json={
            "code": "0",
            "result": {
                "detail": [
                    {"currency": "ADA", "tradeType": "spot", "totalCommission": "0.28452"},
                    {"currency": "USDT", "tradeType": "futures", "totalCommission": "9.91173711"},
                ]
            },
        }
    )
    adapter = _adapter(BitunixAdapter)
    try:
        result = await adapter.get_commission("123", _START, _END)
    finally:
        await adapter.aclose()
    assert [(x.asset, x.amount, x.source) for x in result.lines] == [
        ("ADA", Decimal("0.28452"), "SPOT"),
        ("USDT", Decimal("9.91173711"), "FUTURES"),
    ]
    assert result.total_usdt is None  # no query-time FX conversion or mixed-asset sum
    request = httpx_mock.get_request()
    assert request.url.host == "partners.bitunix.com"
    assert request.url.params["userType"] == "agent"
    assert request.url.params["uid"] == "123"
    assert request.headers["apiKey"] == "api-key"
    assert "signature" in request.headers


async def test_bitunix_slices_180_day_windows(httpx_mock):
    for _ in range(2):
        httpx_mock.add_response(json={"code": "0", "result": {"detail": []}})
    adapter = _adapter(BitunixAdapter)
    end = _DAY + timedelta(days=180)
    try:
        await adapter.get_commission("123", _START, day_end(end))
    finally:
        await adapter.aclose()
    queries = [r.url.params for r in httpx_mock.get_requests()]
    assert len(queries) == 2
    assert queries[0]["endTime"] == "2026-12-27T23:59:59Z"
    assert queries[1]["startTime"] == "2026-12-28T00:00:00Z"


async def test_bitunix_downline_without_uid(httpx_mock):
    httpx_mock.add_response(json={"code": "0", "result": {"totalCommission": "123.456"}})
    adapter = _adapter(BitunixAdapter)
    try:
        assert await adapter.get_downline_total(_START, _END) == Decimal("123.456")
    finally:
        await adapter.aclose()
    assert "uid" not in httpx_mock.get_request().url.params


@pytest.mark.parametrize(
    "verdict, expected",
    [
        (True, ReferralCheck.CONFIRMED),
        (False, ReferralCheck.UNVERIFIABLE),
    ],
)
async def test_bitunix_referral_uses_signed_form(httpx_mock, verdict, expected):
    httpx_mock.add_response(json={"code": "0", "result": {"result": verdict}})
    adapter = _adapter(BitunixAdapter)
    try:
        assert await adapter.verify_referral("123") is expected
    finally:
        await adapter.aclose()
    request = httpx_mock.get_request()
    assert request.method == "POST"
    assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert parse_qs(request.content.decode())["account"] == ["123"]


async def test_bitbase_signature_and_spot_futures_not_double_counted(httpx_mock, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1782907200)
    httpx_mock.add_response(json=_bitbase_response(_bitbase_row()))
    adapter = _adapter(BitbaseAdapter)
    try:
        result = await adapter.get_commission("123", _START, _END)
    finally:
        await adapter.aclose()
    assert result.total_usdt == Decimal("2.239339636179")
    assert sum((line.amount for line in result.lines), Decimal(0)) == result.total_usdt
    request = httpx_mock.get_request()
    assert request.url.host == "www.bitbase.com"
    assert request.headers["validate-signature"] == (
        "a131cda4bb8a4393889324b1fc31b9c63fe2edf0062db0ac79137d468ebb7f98"
    )
    assert request.headers["validate-timestamp"] == "1782907200000"
    assert request.headers["validate-recvwindow"] == "60000"
    assert request.url.query.decode() == (
        "endDate=2026-07-01&role=ALL&startDate=2026-07-01&userId=123"
    )


async def test_bitbase_slices_31_inclusive_days(httpx_mock):
    for _ in range(2):
        httpx_mock.add_response(json=_bitbase_response(_bitbase_row(spot="1", futures="2")))
    adapter = _adapter(BitbaseAdapter)
    # All dates before the frozen current date; 32 days => 31 + 1.
    try:
        result = await adapter.get_commission(
            "123", day_start(date(2026, 6, 1)), day_end(date(2026, 7, 2))
        )
    finally:
        await adapter.aclose()
    assert result.total_usdt == Decimal(6)
    requests = httpx_mock.get_requests()
    assert requests[0].url.params["endDate"] == "2026-07-01"
    assert requests[1].url.params["startDate"] == "2026-07-02"


async def test_bitbase_downline_pages_by_total_not_pagecount(httpx_mock):
    for uid in ("123", "456"):
        httpx_mock.add_response(
            json=_bitbase_response(
                {
                    "items": [_bitbase_row(uid, spot="1", futures="2")],
                    "totalCount": 2,
                    "pageCount": 1,
                }
            )
        )
    adapter = _adapter(BitbaseAdapter)
    try:
        assert await adapter.get_downline_total(_START, _END) == Decimal(6)
    finally:
        await adapter.aclose()
    assert [r.url.params["page"] for r in httpx_mock.get_requests()] == ["1", "2"]


async def test_bitbase_downline_today_unavailable_without_partial_sum(httpx_mock):
    adapter = _adapter(BitbaseAdapter)
    try:
        assert await adapter.get_downline_total(_START, day_end(_NOW.date())) is None
    finally:
        await adapter.aclose()
    assert not httpx_mock.get_requests()


@pytest.mark.parametrize(
    "result",
    [
        {"items": [], "totalCount": 2},
        {"items": [], "totalCount": 10001},
    ],
)
async def test_bitbase_downline_incomplete_or_over_cap_fails(httpx_mock, result):
    httpx_mock.add_response(json=_bitbase_response(result))
    adapter = _adapter(BitbaseAdapter)
    try:
        with pytest.raises(ExchangeApiError):
            await adapter.get_downline_total(_START, _END)
    finally:
        await adapter.aclose()


async def test_bitbase_inconsistent_total_fails(httpx_mock):
    row = _bitbase_row()
    row["commissionAmountU"] = "1000"
    httpx_mock.add_response(json=_bitbase_response(row))
    adapter = _adapter(BitbaseAdapter)
    try:
        with pytest.raises(ExchangeApiError, match="does not match"):
            await adapter.get_commission("123", _START, _END)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "payload, expected",
    [
        (_bitbase_response({"userId": "123"}), ReferralCheck.CONFIRMED),
        ({"rc": 1, "mc": "COSMOS_1012", "result": None}, ReferralCheck.UNVERIFIABLE),
    ],
)
async def test_bitbase_membership_independent_of_money(httpx_mock, payload, expected):
    httpx_mock.add_response(json=payload)
    adapter = _adapter(BitbaseAdapter)
    try:
        assert await adapter.verify_referral("123") is expected
    finally:
        await adapter.aclose()


async def test_blofin_signature_and_commission_not_cashback_or_fee(httpx_mock, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1782907200)
    monkeypatch.setattr(blofin, "uuid4", lambda: "fixed-nonce")
    httpx_mock.add_response(
        json={
            "code": "0",
            "data": [
                {
                    "uid": "123",
                    "commissionTime": to_millis(_START),
                    "commission": "0.032035434",
                    "cashback": "0.288318906",
                    "fee": "3.2035434",
                }
            ],
        }
    )
    adapter = _adapter(BlofinAdapter)
    try:
        result = await adapter.get_commission("123", _START, _END)
    finally:
        await adapter.aclose()
    assert result.total_usdt == Decimal("0.032035434")
    request = httpx_mock.get_request()
    assert request.headers["ACCESS-SIGN"] == (
        "MzliYjE1ZTQxMWQwMjlhZjdmMDk2NTUzNDJmMWVhYzg0MGMwMmI2OWIwZDk4YzllMWVlNTEyMmYzMjFhMWRmYg=="
    )
    assert request.headers["ACCESS-PASSPHRASE"] == "pass"
    assert request.headers["ACCESS-NONCE"] == "fixed-nonce"


async def test_blofin_paginates_daily_time_cursor(httpx_mock, monkeypatch):
    monkeypatch.setattr(blofin, "_PAGE_SIZE", 2)

    def row(day):
        return {"uid": "123", "commission": "0.1", "commissionTime": to_millis(day_start(day))}

    httpx_mock.add_response(
        json={"code": "0", "data": [row(date(2026, 7, 3)), row(date(2026, 7, 2))]}
    )
    httpx_mock.add_response(json={"code": "0", "data": [row(_DAY)]})
    adapter = _adapter(BlofinAdapter)
    try:
        result = await adapter.get_commission("123", _START, day_end(date(2026, 7, 3)))
    finally:
        await adapter.aclose()
    assert result.total_usdt == Decimal("0.3")
    assert result.raw_records_count == 3
    assert (
        int(httpx_mock.get_requests()[1].url.params["end"])
        == to_millis(day_start(date(2026, 7, 2))) - 1
    )


async def test_blofin_mismatched_uid_is_not_income(httpx_mock):
    httpx_mock.add_response(
        json={
            "code": "0",
            "data": [
                {
                    "uid": "999",
                    "commissionTime": to_millis(_START),
                    "commission": "100",
                }
            ],
        }
    )
    adapter = _adapter(BlofinAdapter)
    try:
        with pytest.raises(ExchangeApiError, match="does not match"):
            await adapter.get_commission("123", _START, _END)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("cls", [BitunixAdapter, BitbaseAdapter, BlofinAdapter])
@pytest.mark.parametrize("uid", ["0", "00123", "123&uid=456", "\uff11\uff12\uff13", "-1"])
async def test_invalid_uid_never_reaches_http(httpx_mock, cls, uid):
    adapter = _adapter(cls)
    try:
        with pytest.raises(ExchangeApiError):
            await adapter.get_commission(uid, _START, _END)
    finally:
        await adapter.aclose()
    assert not httpx_mock.get_requests()


@pytest.mark.parametrize("value", [None, True, 0.1, "NaN", "Infinity", "not money"])
def test_invalid_money_is_not_zero(value):
    with pytest.raises(ExchangeApiError):
        amount_of(value)


@pytest.mark.parametrize(
    "cls,payload",
    [
        (BitunixAdapter, {"code": "0", "result": {}}),
        (BitbaseAdapter, _bitbase_response({"userId": "123"})),
        (
            BlofinAdapter,
            {"code": "0", "data": [{"uid": "123", "commissionTime": str(to_millis(_START))}]},
        ),
    ],
)
async def test_partial_success_fails_closed(httpx_mock, cls, payload):
    httpx_mock.add_response(json=payload)
    adapter = _adapter(cls)
    try:
        with pytest.raises(ExchangeApiError):
            await adapter.get_commission("123", _START, _END)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("code", ["bitbase", "bitunix", "blofin"])
def test_secret_aliases_and_canonical_precedence(code):
    settings = make_settings(**{f"{code.upper()}_SECRET_KEY": "alias"})
    assert getattr(settings, f"{code}_api_secret").get_secret_value() == "alias"
    settings = make_settings(
        **{f"{code.upper()}_SECRET_KEY": "alias", f"{code.upper()}_API_SECRET": "canonical"}
    )
    assert getattr(settings, f"{code}_api_secret").get_secret_value() == "canonical"


@pytest.mark.parametrize("code", ["bitbase", "bitunix", "blofin"])
def test_blank_credentials_are_unavailable(code):
    settings = make_settings(**{f"{code.upper()}_API_KEY": " ", f"{code.upper()}_API_SECRET": ""})
    assert code not in settings.available_exchanges
    assert build_adapter(settings, code) is None


def test_blofin_requires_explicit_asset_and_passphrase():
    values = {"BLOFIN_API_KEY": "key", "BLOFIN_API_SECRET": "fake-secret"}
    assert "blofin" not in make_settings(**values).available_exchanges
    values["BLOFIN_API_PASSPHRASE"] = "fake-pass"
    assert "blofin" not in make_settings(**values).available_exchanges
    values["BLOFIN_COMMISSION_ASSET"] = "usdt"
    assert "blofin" in make_settings(**values).available_exchanges


async def test_registry_three_adapters_respect_pauses():
    settings = make_settings(
        **{
            **{
                f"{code}_{part}": "fake"
                for code in ("BITUNIX", "BITBASE", "BLOFIN")
                for part in ("API_KEY", "API_SECRET")
            },
            "BLOFIN_API_PASSPHRASE": "pass",
            "BLOFIN_COMMISSION_ASSET": "USDT",
            "DISABLED_EXCHANGES": "bitunix",
        }
    )
    adapters = build_adapters(settings)
    try:
        assert set(adapters) == {"bitbase", "blofin"}
        assert build_adapter(settings, "bitunix") is None
        assert all(a.settlement_lag_days == 1 for a in adapters.values())
    finally:
        for adapter in adapters.values():
            await adapter.aclose()


def test_uid_choices_require_configured_credentials_and_asset():
    from bot.botui.keyboards import UidExchangeCB, uid_exchange_keyboard

    settings = make_settings(
        **{
            "BITUNIX_API_KEY": "key",
            "BITUNIX_API_SECRET": "secret",
            "BITBASE_API_KEY": "key",
            "BITBASE_API_SECRET": "secret",
            "BLOFIN_API_KEY": "key",
            "BLOFIN_API_SECRET": "secret",
            "BLOFIN_API_PASSPHRASE": "pass",
        }
    )
    choices = {
        UidExchangeCB.unpack(b.callback_data).code
        for row in uid_exchange_keyboard(settings=settings).inline_keyboard
        for b in row
    }
    assert choices == {"bitbase", "bitunix"}


@pytest.mark.parametrize(
    "cls,payload,stamp",
    [
        (BitunixAdapter, {"code": "0", "result": {"detail": []}}, None),
        (BitbaseAdapter, _bitbase_response(_bitbase_row()), "validate-timestamp"),
        (BlofinAdapter, {"code": "0", "data": []}, "ACCESS-TIMESTAMP"),
    ],
)
async def test_retry_refreshes_short_lived_auth(httpx_mock, monkeypatch, cls, payload, stamp):
    wall = [_NOW.timestamp()]
    monkeypatch.setattr("time.time", lambda: wall[0])

    def timeout(request):
        wall[0] += 65
        raise httpx.ReadTimeout("simulated", request=request)

    async def no_backoff(attempt):
        return None

    httpx_mock.add_callback(timeout)
    httpx_mock.add_response(json=payload)
    adapter = _adapter(cls, HTTP_MAX_RETRIES="1")
    monkeypatch.setattr(adapter, "_backoff", no_backoff)
    try:
        await adapter.get_commission("123", _START, _END)
    finally:
        await adapter.aclose()
    first, second = httpx_mock.get_requests()
    if stamp is None:
        assert int(second.url.params["timestamp"]) - int(first.url.params["timestamp"]) == 65
    else:
        assert int(second.headers[stamp]) - int(first.headers[stamp]) == 65000


@pytest.mark.parametrize(
    "cls,body",
    [
        (BitbaseAdapter, {"rc": 1, "mc": "AUTH_103", "ma": ["private-secret"]}),
        (BlofinAdapter, {"code": "152405", "msg": "private-secret"}),
    ],
)
async def test_auth_errors_do_not_echo_body(httpx_mock, cls, body):
    httpx_mock.add_response(status_code=400, json=body)
    adapter = _adapter(cls)
    try:
        with pytest.raises(ExchangeAuthError) as error:
            await adapter.get_commission("123", _START, _END)
        assert "private-secret" not in str(error.value)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "cls,body",
    [
        (BitunixAdapter, {"code": "0", "result": {"detail": []}}),
        (BlofinAdapter, {"code": "0", "data": []}),
    ],
)
async def test_exact_utc8_day_is_not_rebuilt_as_utc_calendar(httpx_mock, cls, body):
    from datetime import timezone

    httpx_mock.add_response(json=body)
    start = datetime(2026, 7, 1, tzinfo=timezone(timedelta(hours=8)))
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    adapter = _adapter(cls)
    try:
        await adapter.get_commission("123", start, end)
    finally:
        await adapter.aclose()
    params = httpx_mock.get_request().url.params
    if cls is BitunixAdapter:
        assert params["startTime"] == "2026-06-30T16:00:00Z"
        assert params["endTime"] == "2026-07-01T15:59:59Z"
    else:
        assert int(params["begin"]) == to_millis(start)
        assert int(params["end"]) == to_millis(end.replace(microsecond=999000))


@pytest.mark.parametrize("days", [1, 30, 31, 60, 180])
@pytest.mark.parametrize("unit", [timedelta(seconds=1), timedelta(milliseconds=1)])
def test_instant_windows_cover_without_overlap(days, unit):
    from bot.exchanges.partner_http import instant_windows

    end = _START + timedelta(days=days) - unit
    windows = list(instant_windows(_START, end, 30, unit=unit))
    assert windows[0][0] == _START and windows[-1][1] == end
    assert all(b - a + unit <= timedelta(days=30) for a, b in windows)
    assert all(a[1] + unit == b[0] for a, b in pairwise(windows))


@pytest.mark.parametrize("cls", [BitunixAdapter, BitbaseAdapter, BlofinAdapter])
async def test_naive_or_reversed_dates_fail_before_http(httpx_mock, cls):
    adapter = _adapter(cls)
    try:
        with pytest.raises(ExchangeApiError):
            await adapter.get_commission("123", _END, _START)
        with pytest.raises(ExchangeApiError):
            await adapter.get_commission(
                "123", _START.replace(tzinfo=None), _END.replace(tzinfo=None)
            )
    finally:
        await adapter.aclose()
    assert not httpx_mock.get_requests()


async def test_bitbase_changing_page_count_cannot_produce_partial_total(httpx_mock):
    for count, uid in [(2, "123"), (3, "456")]:
        httpx_mock.add_response(
            json=_bitbase_response(
                {
                    "items": [_bitbase_row(uid)],
                    "totalCount": count,
                    "pageCount": 1,
                }
            )
        )
    adapter = _adapter(BitbaseAdapter)
    try:
        with pytest.raises(ExchangeApiError, match="count changed"):
            await adapter.get_downline_total(_START, _END)
    finally:
        await adapter.aclose()
