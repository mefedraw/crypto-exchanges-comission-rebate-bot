"""WEEX affiliate commission adapter.

Endpoint: GET /api/v3/rebate/affiliate/getAffiliateCommission
Host: https://api-spot.weex.com
Docs: https://www.weex.com/api-doc/partner/rebate-endpoints/GetAffiliateCommission

VERIFIED (live): host, v3 path (per WEEX support), Bitget-style signing, affiliate
permission, request params (uid/startTime/endTime/coin/productType/page/pageSize),
and the response shape — records under `channelCommissionInfoItems` with
`commission`/`coin` per record, pagination via `pages`. productType defaults to
SPOT and returns one type per call, so we query SPOT and FUTURES and sum; coin is
filtered to USDT. Max range is 3 months; we slice into 30-day windows.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import (
    CommissionResult,
    ExchangeApiError,
    ExchangeAuthError,
    ReferralCheck,
)
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_base64
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

# VERIFIED (live): host https://api-spot.weex.com, path version v3 (per WEEX support).
# Auth + affiliate permission confirmed (v2 returned 40022; v3 authorises and reaches
# the partner system — uid must be a real channel UID).
_PATH = "/api/v3/rebate/affiliate/getAffiliateCommission"
_PAGE_LIMIT = 100
# VERIFIED (live): per-record commission in `commission`, coin in `coin`.
_AMOUNT_FIELDS = ("commission", "commissionAmount", "amount", "rebateAmount")
_COIN_FIELDS = ("coin", "commissionCoin", "asset", "currency")
# productType defaults to SPOT and returns only one type per call — query both
# and sum, otherwise FUTURES commission is silently missed.
_PRODUCT_TYPES = ("SPOT", "FUTURES")
# VERIFIED (live): the affiliate's invited-user list, uid-filterable. With a uid
# filter WEEX answers authoritatively and amount-independently: HTTP 200 iff the uid
# IS the partner's referral (rows under `channelUserInfoItemList`), else business
# code -1054 "The UID is not part of the partner system" — returned, unusually, as
# HTTP 500. So this is a hard membership check: 200 -> CONFIRMED, -1054 ->
# NOT_REFERRAL. Because -1054 rides a 500 (which the shared _send would retry and
# bury), verify_referral calls the client directly and classifies the body.
_AFFILIATE_UIDS_PATH = "/api/v3/rebate/affiliate/getAffiliateUIDs"
_VERIFY_WINDOW_DAYS = 90  # endpoint requires a window; membership is window-independent.
_NOT_PARTNER_CODE = "-1054"  # VERIFIED (live): "The UID is not part of the partner system".


class WeexAdapter(BaseHttpAdapter):
    name = "WEEX"
    code = "weex"
    base_url = "https://api-spot.weex.com"  # VERIFIED (live): spot REST host.
    # VERIFIED (live): API allows up to a 3-month range; we slice into 30-day
    # windows conservatively. Per-record list => additive, exact per period.
    max_window_days = 30
    supports_uid_filter = True
    supports_date_range = True

    def _headers(self, method: str, request_path: str) -> dict[str, str]:
        """VERIFIED (live): Bitget-style base64(hmac_sha256(ts + method + requestPath + body)),
        ms timestamp. Confirmed by the API authenticating the request (it reached the
        permission check; the test key merely lacked the affiliate scope)."""
        timestamp = str(int(time.time() * 1000))
        payload = f"{timestamp}{method.upper()}{request_path}"
        sign = hmac_base64(self._creds.api_secret.get_secret_value(), payload)
        passphrase = self._creds.passphrase.get_secret_value() if self._creds.passphrase else ""
        return {
            "ACCESS-KEY": self._creds.api_key.get_secret_value(),
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
        }

    def _business_error_body(self, response: httpx.Response) -> object | None:
        """WEEX delivers -1054 "uid not under current channel" as HTTP 500 — a business
        response, not an outage. Surface it so the shared ``_send`` returns it (then
        ``_extract_page`` maps -1054 to an empty page: the uid simply earns nothing)
        instead of retrying the 500 and burying the code as ExchangeUnavailableError."""
        body = self._safe_json(response)
        if isinstance(body, dict) and str(body.get("code")) == _NOT_PARTNER_CODE:
            return body
        return None

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), self.max_window_days
        ):
            await self._collect_window(uid, window_start, window_end, result)

        result.settlement_note = "WEEX: сумма spot+futures USDT-комиссии за период (по данным API)."
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> None:
        # USDT only (per requirement); query both product types and sum.
        for product_type in _PRODUCT_TYPES:
            await self._collect_product(uid, window_start, window_end, product_type, result)

    async def _collect_product(
        self,
        uid: str,
        window_start: datetime,
        window_end: datetime,
        product_type: str,
        result: CommissionResult,
    ) -> None:
        page = 1
        while True:
            params = {
                "uid": uid,
                "startTime": to_millis(window_start),
                "endTime": to_millis(window_end),
                "coin": "USDT",
                "productType": product_type,
                "page": page,
                "pageSize": _PAGE_LIMIT,
            }
            query = urlencode(params)
            request_path = f"{_PATH}?{query}"
            payload = await self._send(
                "GET", request_path, headers=self._headers("GET", request_path), log_path=_PATH
            )
            records, total_pages = self._extract_page(payload)
            for record in records:
                asset = self._first(record, _COIN_FIELDS)
                amount_raw = self._first(record, _AMOUNT_FIELDS)
                if asset is None or amount_raw is None:
                    continue
                result.add_amount(str(asset), parse_decimal(amount_raw))
            result.raw_records_count += len(records)

            if not records or len(records) < _PAGE_LIMIT or page >= total_pages:
                break
            page += 1

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT rebate via list-then-sum (like Bitget): enumerate every
        channel uid, then sum each one's spot+futures USDT commission for the period.

        VERIFIED (live 2026-07-20): ``getAffiliateUIDs`` called WITHOUT a uid filter
        returns HTTP 200 with the whole channel under ``channelUserInfoItemList`` (each
        row carries ``uid``), paginated via ``pages`` — so enumeration is real, not
        assumed. A uid that earned nothing yields an empty (or -1054) commission page,
        which :meth:`get_commission` already treats as 0."""
        total = Decimal(0)
        for uid in await self._list_channel_uids(date_from, date_to):
            result = await self.get_commission(uid, date_from, date_to)
            total += sum((line.amount for line in result.lines if line.asset == "USDT"), Decimal(0))
        return total

    async def _list_channel_uids(self, date_from: datetime, date_to: datetime) -> list[str]:
        """Every uid under the owner's channel over the period (deduped, sorted).

        Pages ``getAffiliateUIDs`` with NO uid filter; windows the range by
        ``max_window_days`` and unions the uids so a >30-day span can't be truncated."""
        uids: set[str] = set()
        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), self.max_window_days
        ):
            page = 1
            while True:
                params = {
                    "startTime": to_millis(window_start),
                    "endTime": to_millis(window_end),
                    "page": page,
                    "pageSize": _PAGE_LIMIT,
                }
                request_path = f"{_AFFILIATE_UIDS_PATH}?{urlencode(params)}"
                payload = await self._send(
                    "GET",
                    request_path,
                    headers=self._headers("GET", request_path),
                    log_path=_AFFILIATE_UIDS_PATH,
                )
                rows, total_pages = self._extract_uid_page(payload)
                for row in rows:
                    uid = row.get("uid")
                    if uid not in (None, ""):
                        uids.add(str(uid))
                if not rows or len(rows) < _PAGE_LIMIT or page >= total_pages:
                    break
                page += 1
        return sorted(uids)

    @staticmethod
    def _extract_uid_page(payload: object) -> tuple[list[dict[str, object]], int]:
        """Return (uid rows, total_pages) from a getAffiliateUIDs response.

        VERIFIED (live): flat object, rows under ``channelUserInfoItemList``, page count
        in ``pages`` (no ``code`` field on success)."""
        if not isinstance(payload, dict):
            return [], 0
        items = payload.get("channelUserInfoItemList")
        if isinstance(items, list):
            return items, WeexAdapter._as_int(payload.get("pages"), default=1)
        return [], 0

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Ask WEEX whether ``uid`` is the partner's referral (uid-filtered call).

        HTTP 200 -> CONFIRMED (a non-partner would instead get code -1054). Body code
        -1054 "not part of the partner system" -> NOT_REFERRAL (authoritative,
        amount-independent). Auth failure -> ExchangeAuthError; anything else ->
        UNVERIFIABLE (fail open). Bypasses _send because -1054 rides an HTTP 500 that
        _send would retry and discard.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_VERIFY_WINDOW_DAYS)
        params = {
            "uid": uid,
            "startTime": to_millis(start),
            "endTime": to_millis(end),
            "page": 1,
            "pageSize": _PAGE_LIMIT,
        }
        request_path = f"{_AFFILIATE_UIDS_PATH}?{urlencode(params)}"
        try:
            resp = await self._client.get(request_path, headers=self._headers("GET", request_path))
        except (httpx.TimeoutException, httpx.TransportError):
            self._log.warning("verify_referral network error", path=_AFFILIATE_UIDS_PATH)
            return ReferralCheck.UNVERIFIABLE

        if resp.status_code == 200:
            return ReferralCheck.CONFIRMED
        body = self._safe_json(resp)
        if isinstance(body, dict) and str(body.get("code")) == _NOT_PARTNER_CODE:
            return ReferralCheck.NOT_REFERRAL
        if resp.status_code in (401, 403):
            raise ExchangeAuthError(f"{self.name}: HTTP {resp.status_code}")
        self._log.warning(
            "verify_referral unexpected response",
            path=_AFFILIATE_UIDS_PATH,
            status=resp.status_code,
        )
        return ReferralCheck.UNVERIFIABLE

    def _extract_page(self, payload: object) -> tuple[list[dict], int]:
        """Return (records, total_pages) from a WEEX v3 commission response."""
        if not isinstance(payload, dict):
            return [], 0
        code = str(payload.get("code", "00000"))
        if code == _NOT_PARTNER_CODE:
            # uid is not under this channel -> no commission for it (WEEX returns this as
            # HTTP 500; _business_error_body surfaces the body here). Not an error.
            return [], 0
        if code not in ("00000", "0", "200"):
            raise ExchangeApiError(f"{self.name}: {code} {payload.get('msg')}")
        # VERIFIED (live): v3 is a flat object — records under
        # `channelCommissionInfoItems`, pagination via `pages`.
        items = payload.get("channelCommissionInfoItems")
        if isinstance(items, list):
            return items, self._as_int(payload.get("pages"), default=1)
        # Fallback for any data.* envelope shape.
        data = payload.get("data")
        if isinstance(data, list):
            return data, 1
        if isinstance(data, dict):
            for key in ("list", "rows", "items", "records"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner, self._as_int(data.get("pages"), default=1)
        return [], 0

    @staticmethod
    def _as_int(value: object, *, default: int) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _first(record: dict, fields: tuple[str, ...]) -> object | None:
        for field in fields:
            if field in record and record[field] not in (None, ""):
                return record[field]
        return None


@register("weex")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> WeexAdapter:
    return WeexAdapter(credentials, settings)
