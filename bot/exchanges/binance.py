"""Binance affiliate (Api-Agent) commission adapter.

Endpoint: GET /sapi/v1/apiReferral/rebate/recentRecord
Docs: https://binance-docs.github.io/apiAgent-API-EN/api_rebate_endpoints_spot_EN/

ELIGIBILITY: this endpoint is ONLY for accounts enrolled in Binance's Api-Agent /
API Partner program — a plain affiliate referral link has NO API access to
commission data (that lives only in the web Affiliate Portal). VERIFIED (live) on
the configured account: the signed request returns HTTP 200 with an (empty, at
setup time) JSON array, i.e. the account HAS apiReferral access; a non-agent
account would get business code -2015 ("invalid API-key ... or permissions").

SHAPE (per docs): the response is a flat JSON ARRAY of per-trade rebate records,
each ``{"customerId", "email", "income", "asset", "symbol", "time", "orderId",
"tradeId"}``. ``income`` is the commission amount in ``asset`` (any coin: USDT,
BNB, ...). Records are per-trade with an absolute ``time``, so summing disjoint
windows is additive (unlike MEXC's per-invitee ``total``).

WINDOW: startTime/endTime must BOTH be set with a span <= 7 days (docs) -> we slice
into <=7-day windows. Signing: HMAC-SHA256 hex over the query string (incl.
``timestamp``/``recvWindow``), appended as ``&signature=``; key in ``X-MBX-APIKEY``.

# ASSUMED (not live-verified — the account has no invitees/commission yet): the
# per-record field names above, that ``customerId`` filters server-side, that a
# 1-day window returns that day's records, and plain-UTC (no UTC+8 shift) attribution.
# Revisit against live data once the first referral trades.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

_PATH = "/sapi/v1/apiReferral/rebate/recentRecord"
_WINDOW_DAYS = 7  # VERIFIED (docs): startTime/endTime span must be <= 7 days.
_LIMIT = 500  # VERIFIED (docs): max 500 records per call; no page/cursor param.
_RECV_WINDOW = 5000
# ASSUMED: the invitee id is `customerId`; extra names kept as a defensive fallback.
_UID_FIELDS = ("customerId", "customerUid", "uid")


class BinanceAdapter(BaseHttpAdapter):
    name = "Binance"
    code = "binance"
    base_url = "https://api.binance.com"  # VERIFIED: Binance spot/sapi REST host.
    max_window_days = _WINDOW_DAYS
    supports_uid_filter = True  # ASSUMED: `customerId` query param filters server-side.
    supports_date_range = True

    def _signed_query(self, params: dict[str, object]) -> str:
        """Binance signing: signature = HMAC-SHA256-hex over the urlencoded query."""
        query = urlencode(params)
        signature = hmac_hex(self._creds.api_secret.get_secret_value(), query)
        return f"{query}&signature={signature}"

    def _headers(self) -> dict[str, str]:
        return {
            "X-MBX-APIKEY": self._creds.api_key.get_secret_value(),
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), self.max_window_days
        ):
            await self._collect_window(uid, window_start, window_end, result)

        result.settlement_note = (
            "Сумма комиссий Binance (apiReferral) за период. Требует Api-Agent доступа; "
            "точные выплаты сверяйтесь с Affiliate Portal."
        )
        return result.finalize()

    async def _collect_window(
        self,
        uid: str | None,
        window_start: datetime,
        window_end: datetime,
        result: CommissionResult,
        usdt_only: bool = False,
    ) -> Decimal:
        """Query one <=7-day window. When ``uid`` is given, keep only that invitee's
        records; when ``usdt_only`` is set, accumulate just the USDT total (for the
        downline total) and return it. Otherwise add every asset line to ``result``."""
        params: dict[str, object] = {
            "startTime": to_millis(window_start),
            "endTime": to_millis(window_end),
            "limit": _LIMIT,
            "recvWindow": _RECV_WINDOW,
            "timestamp": int(time.time() * 1000),
        }
        if uid is not None:
            params["customerId"] = uid  # ASSUMED (docs): server-side per-invitee filter.
        request_path = f"{_PATH}?{self._signed_query(params)}"
        payload = await self._send("GET", request_path, headers=self._headers(), log_path=_PATH)
        rows = self._extract_rows(payload)
        if len(rows) >= _LIMIT:
            # No pagination on this endpoint: a full page may mean truncation.
            self._log.warning("binance rebate page full — possible truncation", count=len(rows))

        usdt_total = Decimal(0)
        for row in rows:
            if uid is not None and not self._row_matches_uid(row, uid):
                continue
            income = row.get("income")
            if income in (None, ""):
                continue
            asset = str(row.get("asset", "")).upper()
            amount = parse_decimal(str(income))
            if usdt_only:
                if asset == "USDT":
                    usdt_total += amount
            else:
                if amount != Decimal(0):
                    result.add_amount(asset or "USDT", amount)
                result.raw_records_count += 1
        return usdt_total

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT rebate: the same endpoint WITHOUT ``customerId`` returns
        every invitee's records; only the USDT bucket is summed (base contract).

        # ASSUMED: omitting ``customerId`` returns all invitees (not live-verified —
        # no invitees yet). Windowed <=7 days and additive over disjoint windows."""
        self._log.info("get_downline_total", date_from=str(date_from), date_to=str(date_to))
        result = self._new_result("", date_from, date_to)
        total = Decimal(0)
        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), self.max_window_days
        ):
            total += await self._collect_window(
                None, window_start, window_end, result, usdt_only=True
            )
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Soft membership check: query the last <=7 days filtered to ``uid``. Any
        record confirms; no record is UNVERIFIABLE (fail open) — the endpoint only
        surfaces invitees who traded recently, so absence is not an authoritative
        "not my referral" (a real referral who simply hasn't traded returns nothing).
        """
        end = datetime.now(UTC)
        begin = end - timedelta(days=_WINDOW_DAYS - 1)
        params: dict[str, object] = {
            "customerId": uid,
            "startTime": to_millis(begin),
            "endTime": to_millis(end),
            "limit": _LIMIT,
            "recvWindow": _RECV_WINDOW,
            "timestamp": int(time.time() * 1000),
        }
        request_path = f"{_PATH}?{self._signed_query(params)}"
        payload = await self._send("GET", request_path, headers=self._headers(), log_path=_PATH)
        rows = self._extract_rows(payload)
        if any(self._row_matches_uid(row, uid) for row in rows):
            return ReferralCheck.CONFIRMED
        return ReferralCheck.UNVERIFIABLE

    def _extract_rows(self, payload: object) -> list[dict[str, object]]:
        """Success is a flat JSON array. A dict with a non-zero ``code`` is a Binance
        business error (e.g. -2015 no permission) — raise so it is not silently 0."""
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None and str(code) not in ("0", "200", "000000"):
                raise ExchangeApiError(f"{self.name}: {code} {payload.get('msg')}")
            data = payload.get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
        return []

    @staticmethod
    def _row_matches_uid(row: dict[str, object], uid: str) -> bool:
        return any(str(row.get(field)) == uid for field in _UID_FIELDS)


@register("binance")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BinanceAdapter:
    return BinanceAdapter(credentials, settings)
