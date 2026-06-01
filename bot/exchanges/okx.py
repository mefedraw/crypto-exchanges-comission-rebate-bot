"""OKX affiliate invitee detail adapter.

Endpoint: GET /api/v5/affiliate/invitee/detail
Docs: https://www.okx.com/docs-v5/en/#affiliate-rest-api

This endpoint returns an invitee's *cumulative* detail, not a per-date-range
sum. We therefore treat the commission as a cumulative reference value and warn
the user (like Bybit), rather than presenting it as an exact period total.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_base64
from bot.utils.money import parse_decimal

_PATH = "/api/v5/affiliate/invitee/detail"
# ASSUMED: candidate field holding the cumulative commission amount.
_COMMISSION_FIELDS = ("accCommission", "totalCommission", "commission", "rebateAmt")


class OkxAdapter(BaseHttpAdapter):
    name = "OKX"
    code = "okx"
    base_url = "https://www.okx.com"  # VERIFIED: OKX REST host.
    max_window_days = 365
    supports_uid_filter = True
    supports_date_range = False  # ASSUMED: endpoint returns cumulative detail.

    def _iso_timestamp(self) -> str:
        # VERIFIED: OKX requires ISO-8601 millis UTC, e.g. 2020-12-08T09:08:57.715Z.
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _headers(self, method: str, request_path: str) -> dict[str, str]:
        """OKX signing: SIGN = base64(hmac_sha256(ts + method + requestPath + body))."""
        # VERIFIED: signature scheme per OKX v5 auth docs.
        timestamp = self._iso_timestamp()
        payload = f"{timestamp}{method.upper()}{request_path}"
        sign = hmac_base64(self._creds.api_secret.get_secret_value(), payload)
        passphrase = self._creds.passphrase.get_secret_value() if self._creds.passphrase else ""
        return {
            "OK-ACCESS-KEY": self._creds.api_key.get_secret_value(),
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": passphrase,
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        query = urlencode({"uid": uid})
        request_path = f"{_PATH}?{query}"
        payload = await self._send(
            "GET", request_path, headers=self._headers("GET", request_path), log_path=_PATH
        )
        rows = self._extract_rows(payload)
        result.raw_records_count = len(rows)

        for row in rows:
            amount_raw = self._first(row, _COMMISSION_FIELDS)
            if amount_raw is not None:
                amount = parse_decimal(amount_raw)
                result.add_amount("USDT", amount)  # ASSUMED: commission denominated in USDT.

        result.notes.append(
            "OKX API отдаёт НАКОПЛЕННУЮ комиссию по рефералу, а не сумму за период. "
            "Значение справочное — за точный период сверяйтесь с кабинетом партнёра."
        )
        result.settlement_note = "Накопленная комиссия OKX (не привязана к диапазону дат)."
        return result.finalize()

    def _extract_rows(self, payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        if str(payload.get("code", "0")) != "0":
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")
        data = payload.get("data")
        return data if isinstance(data, list) else []

    @staticmethod
    def _first(record: dict, fields: tuple[str, ...]) -> object | None:
        for field in fields:
            if field in record and record[field] not in (None, ""):
                return record[field]
        return None


@register("okx")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> OkxAdapter:
    return OkxAdapter(credentials, settings)
