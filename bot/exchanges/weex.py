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
from datetime import datetime
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError
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
            "WEEX: сумма spot+futures USDT-комиссии за период (по данным API)."
        )
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

    def _extract_page(self, payload: object) -> tuple[list[dict], int]:
        """Return (records, total_pages) from a WEEX v3 commission response."""
        if not isinstance(payload, dict):
            return [], 0
        code = str(payload.get("code", "00000"))
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
