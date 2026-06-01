"""KuCoin affiliate commission adapter.

Endpoint: GET /api/v2/affiliate/queryMyCommission
Docs: https://www.kucoin.com/docs-new/rest/affiliate/get-commission

Reliable spec (docs include the response body). Sum `commission` by `currency`.
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

_PATH = "/api/v2/affiliate/queryMyCommission"
_PAGE_SIZE = 100  # VERIFIED (live): page/pageSize pagination, items under data.items.


class KucoinAdapter(BaseHttpAdapter):
    name = "KuCoin"
    code = "kucoin"
    base_url = "https://api.kucoin.com"  # VERIFIED: KuCoin REST host.
    # Per-record payout commissions are additive across windows (live-verified);
    # 30-day chunks are a safe default (KuCoin accepted them).
    max_window_days = 30
    supports_uid_filter = True
    supports_date_range = True

    def _headers(self, method: str, endpoint: str) -> dict[str, str]:
        """KuCoin v2 signing (HMAC-SHA256 base64; passphrase is also signed)."""
        # VERIFIED: SIGN = base64(hmac_sha256(timestamp + method + endpoint + body)).
        timestamp = str(int(time.time() * 1000))
        secret = self._creds.api_secret.get_secret_value()
        passphrase = self._creds.passphrase.get_secret_value() if self._creds.passphrase else ""
        sign = hmac_base64(secret, f"{timestamp}{method}{endpoint}")
        signed_passphrase = hmac_base64(secret, passphrase)
        return {
            "KC-API-KEY": self._creds.api_key.get_secret_value(),
            "KC-API-SIGN": sign,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": signed_passphrase,
            "KC-API-KEY-VERSION": "2",
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

        result.settlement_note = "Сумма комиссий KuCoin за период (по данным API)."
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> None:
        page = 1
        while True:
            params = {
                "userId": uid,
                "rebateStartAt": to_millis(window_start),
                "rebateEndAt": to_millis(window_end),
                "page": page,
                "pageSize": _PAGE_SIZE,
            }
            query = urlencode(params)
            endpoint = f"{_PATH}?{query}"
            payload = await self._send(
                "GET", endpoint, headers=self._headers("GET", endpoint), log_path=_PATH
            )
            self._check_envelope(payload)
            items = self._extract_items(payload)
            for item in items:
                # VERIFIED (live): each item exposes `commission` + `currency`.
                amount = parse_decimal(item["commission"])
                asset = str(item["currency"])
                result.add_amount(asset, amount)
            result.raw_records_count += len(items)

            if len(items) < _PAGE_SIZE:
                break
            page += 1

    def _check_envelope(self, payload: object) -> None:
        if isinstance(payload, dict) and str(payload.get("code")) not in ("200000", "None"):
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")

    @staticmethod
    def _extract_items(payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return items
        return []


@register("kucoin")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> KucoinAdapter:
    return KucoinAdapter(credentials, settings)
