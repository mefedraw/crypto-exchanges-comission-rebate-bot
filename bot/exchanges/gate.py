"""Gate (gate.io / gate.com) affiliate commission adapter.

Endpoint: GET /api/v4/rebate/partner/commission_history
Docs: https://www.gate.com/docs/developers/apiv4/en/

The most reliable spec of the seven: a clean list of per-record commissions that
we sum by asset ourselves.
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex, sha512_hex
from bot.utils.dates import iter_windows, to_seconds
from bot.utils.money import parse_decimal

_PATH = "/api/v4/rebate/partner/commission_history"
_PAGE_LIMIT = 100  # VERIFIED: Gate accepts limit up to 1000; 100 keeps pages small.


class GateAdapter(BaseHttpAdapter):
    name = "Gate"
    code = "gate"
    base_url = "https://api.gateio.ws"  # VERIFIED: Gate v4 REST host.
    max_window_days = 30  # VERIFIED: docs cap the from/to range at 30 days.
    supports_uid_filter = True
    supports_date_range = True

    def _headers(self, method: str, query: str) -> dict[str, str]:
        """Gate v4 signing: SIGN = HMAC-SHA512(method\\npath\\nquery\\nSHA512(body)\\nts)."""
        # VERIFIED: signature scheme per Gate v4 "APIv4 Signed Request" docs.
        timestamp = str(int(time.time()))
        hashed_body = sha512_hex("")  # GET has no body.
        payload = f"{method}\n{_PATH}\n{query}\n{hashed_body}\n{timestamp}"
        signature = hmac_hex(
            self._creds.api_secret.get_secret_value(), payload, algorithm="sha512"
        )
        return {
            "KEY": self._creds.api_key.get_secret_value(),
            "SIGN": signature,
            "Timestamp": timestamp,
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
            "Сумма начислений по записям Gate за период (по данным API)."
        )
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> None:
        offset = 0
        while True:
            params = {
                "user_id": uid,
                "from": to_seconds(window_start),
                "to": to_seconds(window_end),
                "limit": _PAGE_LIMIT,
                "offset": offset,
            }
            query = urlencode(params)
            data = await self._send(
                "GET", f"{_PATH}?{query}", headers=self._headers("GET", query), log_path=_PATH
            )
            records = self._extract_records(data)
            for record in records:
                # ASSUMED: field names commission_amount/commission_asset per docs;
                # verify against a live response before production use.
                amount = parse_decimal(record["commission_amount"])
                asset = str(record["commission_asset"])
                source = record.get("source")
                result.add_amount(asset, amount, source=str(source) if source else None)
            result.raw_records_count += len(records)

            if len(records) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT

    @staticmethod
    def _extract_records(data: object) -> list[dict]:
        """Gate may return a bare list or a {"list": [...]} envelope."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("list")
            if isinstance(inner, list):
                return inner
        return []


@register("gate")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> GateAdapter:
    return GateAdapter(credentials, settings)
