"""BitMart futures affiliate rebate adapter.

BitMart's documented single-user endpoint
``/contract/private/affiliate/rebate-user`` returns ``rebate`` without a currency
field. The futures affiliate list endpoint returns the same data in the useful
per-currency form (BTC/USDT/ETH), so this adapter queries ``rebate-list`` with a
single ``user_id`` filter and one request per rebate currency.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import (
    CommissionResult,
    ExchangeApiError,
    ExchangeAuthError,
    ExchangeRateLimitError,
)
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.utils.dates import iter_windows, to_seconds
from bot.utils.money import parse_decimal

_PATH = "/contract/private/affiliate/rebate-list"
_PAGE_LIMIT = 50
# VERIFIED (official BitMart Futures V2 docs): futures assets are USDT
# (U-native), BTC and ETH (coin-native); rebate-list exposes one currency filter.
_REBATE_CURRENCIES = ("USDT", "BTC", "ETH")
_AMOUNT_FIELDS = ("total_rebate_amount", "rebate", "rebate_amount", "amount")
_COIN_FIELDS = ("rebate_coin", "coin", "currency", "asset")
_UID_FIELDS = ("trade_user_id", "user_id", "cid")
_AUTH_ERROR_CODES = {
    "30001",
    "30002",
    "30003",
    "30004",
    "30005",
    "30006",
    "30007",
    "30008",
    "30010",
    "30011",
    "30012",
    "30019",
}
_RATE_LIMIT_CODES = {"30013", "30017"}


class BitmartAdapter(BaseHttpAdapter):
    name = "BitMart"
    code = "bitmart"
    base_url = "https://api-cloud-v2.bitmart.com"
    max_window_days = 60  # VERIFIED: affiliate rebate time interval cap is 60 days.
    supports_uid_filter = True
    supports_date_range = True

    def _headers(self) -> dict[str, str]:
        """KEYED BitMart endpoints require the API access key in ``X-BM-KEY``."""
        # VERIFIED (official docs): authentication type KEYED only requires
        # X-BM-KEY; the endpoint is rate-limited by X-BM-KEY.
        return {
            "X-BM-KEY": self._creds.api_key.get_secret_value(),
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
            for currency in _REBATE_CURRENCIES:
                await self._collect_currency(uid, window_start, window_end, currency, result)

        result.settlement_note = (
            "BitMart Futures: сумма рефбека по BTC/USDT/ETH за период "
            "(по данным API)."
        )
        return result.finalize()

    async def _collect_currency(
        self,
        uid: str,
        window_start: datetime,
        window_end: datetime,
        currency: str,
        result: CommissionResult,
    ) -> None:
        page = 1
        records_seen = False
        fallback_sum: Decimal | None = None

        while True:
            params: dict[str, object] = {
                "user_id": uid,
                "page": page,
                "size": _PAGE_LIMIT,
                "currency": currency,
                "rebate_start_time": to_seconds(window_start),
                "rebate_end_time": to_seconds(window_end),
            }
            query = urlencode(params)
            payload = await self._send(
                "GET", f"{_PATH}?{query}", headers=self._headers(), log_path=_PATH
            )
            records, total, top_level_sum = self._extract_page(payload, currency)
            if page == 1:
                fallback_sum = top_level_sum
            if records:
                records_seen = True

            for record in records:
                if not self._record_matches_uid(record, uid):
                    continue
                amount_raw = self._first(record, _AMOUNT_FIELDS)
                if amount_raw is None:
                    continue
                asset_raw = self._first(record, _COIN_FIELDS) or currency
                result.add_amount(str(asset_raw).upper(), self._parse_decimal(amount_raw))
            result.raw_records_count += len(records)

            if len(records) < _PAGE_LIMIT or (total > 0 and page * _PAGE_LIMIT >= total):
                break
            page += 1

        # Some BitMart responses may expose only btc/usdt/eth_rebate_sum. Use that
        # only when no detail rows came back, so we never double-count a page.
        if not records_seen and fallback_sum is not None and fallback_sum != Decimal(0):
            result.add_amount(currency, fallback_sum)

    def _extract_page(
        self, payload: object, currency: str
    ) -> tuple[list[dict[str, object]], int, Decimal | None]:
        if not isinstance(payload, dict):
            return [], 0, None
        data = self._unwrap_payload(payload)
        records = self._records_from(data)
        total = self._as_int(data.get("total"), default=len(records))
        return records, total, self._extract_top_level_sum(data, currency)

    def _unwrap_payload(self, payload: dict[object, object]) -> dict[str, object]:
        if "code" in payload:
            code = str(payload.get("code"))
            if code != "1000":
                self._raise_business_error(code, payload.get("message") or payload.get("msg"))
            data = payload.get("data")
            if isinstance(data, dict):
                return {str(key): value for key, value in data.items()}
        if payload.get("success") is False:
            raise ExchangeApiError(f"{self.name}: {payload.get('message')}")
        return {str(key): value for key, value in payload.items()}

    @staticmethod
    def _records_from(data: dict[str, object]) -> list[dict[str, object]]:
        for key in ("rebate_detail_page_data", "list", "items", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return [
                    {str(row_key): row_value for row_key, row_value in row.items()}
                    for row in value
                    if isinstance(row, dict)
                ]
        return []

    @staticmethod
    def _extract_top_level_sum(data: dict[str, object], currency: str) -> Decimal | None:
        value = data.get(f"{currency.lower()}_rebate_sum")
        if value in (None, ""):
            return None
        return BitmartAdapter._parse_decimal(value)

    @staticmethod
    def _record_matches_uid(record: dict[str, object], uid: str) -> bool:
        for field in _UID_FIELDS:
            value = record.get(field)
            if value not in (None, ""):
                return str(value) == uid
        return True

    @staticmethod
    def _first(record: dict[str, object], fields: tuple[str, ...]) -> object | None:
        for field in fields:
            if field in record and record[field] not in (None, ""):
                return record[field]
        return None

    @staticmethod
    def _as_int(value: object, *, default: int) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, Decimal | float | str):
            try:
                return int(value)
            except ValueError:
                return default
        return default

    @staticmethod
    def _parse_decimal(value: object) -> Decimal:
        if isinstance(value, str | int | float | Decimal):
            return parse_decimal(value)
        raise ExchangeApiError(f"BitMart: cannot parse amount {value!r}")

    def _raise_business_error(self, code: str, message: object) -> None:
        text = f"{self.name}: {code} {message}"
        if code in _RATE_LIMIT_CODES:
            raise ExchangeRateLimitError(text)
        if code in _AUTH_ERROR_CODES:
            raise ExchangeAuthError(text)
        raise ExchangeApiError(text)

    def _raise_client_error(self, status: int, response: httpx.Response) -> None:
        if status == 418:
            raise ExchangeRateLimitError(f"{self.name}: HTTP 418")
        body = self._safe_json(response)
        if isinstance(body, dict) and "code" in body:
            self._raise_business_error(
                str(body.get("code")), body.get("message") or body.get("msg")
            )
        super()._raise_client_error(status, response)


@register("bitmart")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BitmartAdapter:
    return BitmartAdapter(credentials, settings)
