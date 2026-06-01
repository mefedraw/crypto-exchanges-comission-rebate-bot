"""WEEX affiliate commission adapter.

Endpoint: GET /api/v2/rebate/affiliate/getAffiliateCommission
Host: https://api-spot.weex.com
Docs: https://www.weex.com/api-doc/spot/rebate-endpoints/GetAffiliateCommission

VERIFIED (live): host, v2 path, Bitget-style signing (the API authenticated our
request), and the {"code":"00000",...} envelope.
STILL ASSUMED: request parameter names (uid/startTime/endTime/pageNo) and the
response field names — they could not be confirmed because the available API key
lacked the affiliate permission (HTTP 403, code 40022). Re-verify the response
shape against live data once a key with affiliate scope is used; a runtime
warning is emitted until then.
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

# VERIFIED (live): host https://api-spot.weex.com, path version v2, envelope code "00000".
_PATH = "/api/v2/rebate/affiliate/getAffiliateCommission"
_PAGE_LIMIT = 100
_AMOUNT_FIELDS = ("commission", "commissionAmount", "amount", "rebateAmount")
_COIN_FIELDS = ("coin", "commissionCoin", "asset", "currency")


class WeexAdapter(BaseHttpAdapter):
    name = "WEEX"
    code = "weex"
    base_url = "https://api-spot.weex.com"  # VERIFIED (live): spot REST host.
    max_window_days = 30  # ASSUMED.
    supports_uid_filter = True  # ASSUMED.
    supports_date_range = True  # ASSUMED.

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

        result.notes.append(
            "WEEX-адаптер НЕ верифицирован по живой документации — результат может быть неточным. "
            "Сверьтесь с кабинетом партнёра WEEX."
        )
        result.settlement_note = "WEEX: спецификация не подтверждена (см. предупреждение выше)."
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> None:
        page = 1
        while True:
            params = {
                "uid": uid,  # ASSUMED param name.
                "startTime": to_millis(window_start),  # ASSUMED date format (ms).
                "endTime": to_millis(window_end),
                "pageNo": page,
                "pageSize": _PAGE_LIMIT,
            }
            query = urlencode(params)
            request_path = f"{_PATH}?{query}"
            payload = await self._send(
                "GET", request_path, headers=self._headers("GET", request_path), log_path=_PATH
            )
            records = self._extract_records(payload)
            for record in records:
                asset = self._first(record, _COIN_FIELDS)
                amount_raw = self._first(record, _AMOUNT_FIELDS)
                if asset is None or amount_raw is None:
                    continue
                result.add_amount(str(asset), parse_decimal(amount_raw))
            result.raw_records_count += len(records)

            if len(records) < _PAGE_LIMIT:
                break
            page += 1

    def _extract_records(self, payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        # ASSUMED: success code "00000" (Bitget-like). Treat anything else as error.
        code = str(payload.get("code", "00000"))
        if code not in ("00000", "0", "200"):
            raise ExchangeApiError(f"{self.name}: {code} {payload.get('msg')}")
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "rows", "items", "records"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner
        return []

    @staticmethod
    def _first(record: dict, fields: tuple[str, ...]) -> object | None:
        for field in fields:
            if field in record and record[field] not in (None, ""):
                return record[field]
        return None


@register("weex")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> WeexAdapter:
    return WeexAdapter(credentials, settings)
