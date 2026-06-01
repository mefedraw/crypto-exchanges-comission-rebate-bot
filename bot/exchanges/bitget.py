"""Bitget agent/broker commission adapter.

Endpoint: GET /api/v2/broker/customer-commissions
Docs: https://www.bitget.com/api-doc/affiliate/customerInfo/GetDirectCommissions

Field names of the amount/coin and the idLessThan pagination behaviour are not
fully pinned down — marked ASSUMED and resolved defensively.
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import urlencode

import httpx

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ExchangeAuthError
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_base64
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

_PATH = "/api/v2/broker/customer-commissions"
_PAGE_LIMIT = 100
# ASSUMED: candidate response field names (verify live).
_AMOUNT_FIELDS = ("commission", "commissionAmount", "amount", "rebateAmount")
_COIN_FIELDS = ("coin", "commissionCoin", "asset", "currency")
_ID_FIELDS = ("id", "billId", "cTime")
# VERIFIED: Bitget returns auth failures as HTTP 400 with these business codes.
_AUTH_ERROR_CODES = {"40009", "40012", "40037", "40006", "40011", "40014"}


class BitgetAdapter(BaseHttpAdapter):
    name = "Bitget"
    code = "bitget"
    base_url = "https://api.bitget.com"  # VERIFIED: Bitget REST host.
    max_window_days = 30  # VERIFIED: 30-day max window.
    supports_uid_filter = True
    supports_date_range = True

    def _headers(self, method: str, request_path: str) -> dict[str, str]:
        """Bitget signing: SIGN = base64(hmac_sha256(ts + method + requestPath + body))."""
        # VERIFIED: signature scheme per Bitget API auth docs.
        timestamp = str(int(time.time() * 1000))
        payload = f"{timestamp}{method.upper()}{request_path}"
        sign = hmac_base64(self._creds.api_secret.get_secret_value(), payload)
        passphrase = self._creds.passphrase.get_secret_value() if self._creds.passphrase else ""
        return {
            "ACCESS-KEY": self._creds.api_key.get_secret_value(),
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": passphrase,
            "locale": "en-US",
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

        result.settlement_note = "Сумма начислений Bitget за период (по данным API)."
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> None:
        id_less_than: str | None = None
        while True:
            params: dict[str, object] = {
                "uid": uid,
                "startTime": to_millis(window_start),
                "endTime": to_millis(window_end),
                "limit": _PAGE_LIMIT,
            }
            if id_less_than is not None:
                params["idLessThan"] = id_less_than
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
            last_id = self._first(records[-1], _ID_FIELDS)
            if last_id is None:
                break  # can't paginate without a cursor; stop to avoid a loop.
            id_less_than = str(last_id)

    def _extract_records(self, payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        if str(payload.get("code", "00000")) != "00000":
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "commissionList", "items"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner
        return []

    def _raise_client_error(self, status: int, response: httpx.Response) -> None:
        body = self._safe_json(response)
        if isinstance(body, dict):
            code = str(body.get("code"))
            if code in _AUTH_ERROR_CODES:
                raise ExchangeAuthError(f"{self.name}: {code} {body.get('msg')}")
        super()._raise_client_error(status, response)

    @staticmethod
    def _first(record: dict, fields: tuple[str, ...]) -> object | None:
        for field in fields:
            if field in record and record[field] not in (None, ""):
                return record[field]
        return None


@register("bitget")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BitgetAdapter:
    return BitgetAdapter(credentials, settings)
