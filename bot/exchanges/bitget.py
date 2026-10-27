"""Bitget agent/broker commission adapter.

Endpoint: GET /api/v2/broker/customer-commissions
Docs: https://www.bitget.com/api-doc/affiliate/customerInfo/GetDirectCommissions

VERIFIED (live): records under data.commissionList; per-record rebate in
`rebateAmount`, coin in `coin`; pagination via the response-level `endId` cursor
passed back as `idLessThan` (records themselves carry no id). The *TotalRebateAmount
fields are running cumulatives and are intentionally not summed.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ExchangeAuthError, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_base64
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

_PATH = "/api/v2/broker/customer-commissions"
# VERIFIED (official Bitget affiliate docs): the broker's customer list, uid-filterable,
# returned independent of rebate — the membership check. POST with a JSON body.
_CUSTOMER_LIST_PATH = "/api/v2/broker/customer-list"
_PAGE_LIMIT = 100
# VERIFIED (live + docs): Bitget buckets the `date` field by UTC+8 day, and the
# partner cabinet filters by UTC+8 calendar dates. We receive UTC day boundaries,
# so shift them back 8h to align the query window with Bitget's UTC+8 days
# (otherwise "01.05–31.05" leaks into 01.06 UTC+8 and over-reports).
_UTC8_OFFSET_MS = 8 * 60 * 60 * 1000
# VERIFIED (live): per-record rebate is `rebateAmount`, coin is `coin`. The
# *TotalRebateAmount fields are running cumulatives and must NOT be summed.
_AMOUNT_FIELDS = ("rebateAmount", "commission", "commissionAmount", "amount")
_COIN_FIELDS = ("coin", "commissionCoin", "asset", "currency")
# VERIFIED: Bitget returns auth failures as HTTP 400 with these business codes.
_AUTH_ERROR_CODES = {"40009", "40012", "40037", "40006", "40011", "40014"}


class BitgetAdapter(BaseHttpAdapter):
    name = "Bitget"
    code = "bitget"
    base_url = "https://api.bitget.com"  # VERIFIED: Bitget REST host.
    max_window_days = 30  # VERIFIED: 30-day max window.
    supports_uid_filter = True
    supports_date_range = True

    def _headers(self, method: str, request_path: str, body: str = "") -> dict[str, str]:
        """Bitget signing: SIGN = base64(hmac_sha256(ts + method + requestPath + body))."""
        # VERIFIED: signature scheme per Bitget API auth docs. GET has no body; POST
        # signs the exact JSON string we also send verbatim as the request content.
        timestamp = str(int(time.time() * 1000))
        payload = f"{timestamp}{method.upper()}{request_path}{body}"
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
                # Shift UTC boundaries to UTC+8 days to match Bitget's bucketing.
                "startTime": to_millis(window_start) - _UTC8_OFFSET_MS,
                "endTime": to_millis(window_end) - _UTC8_OFFSET_MS,
                "limit": _PAGE_LIMIT,
            }
            if id_less_than is not None:
                params["idLessThan"] = id_less_than
            query = urlencode(params)
            request_path = f"{_PATH}?{query}"
            payload = await self._send(
                "GET", request_path, headers=self._headers("GET", request_path), log_path=_PATH
            )
            records, end_id = self._extract_page(payload)
            for record in records:
                asset = self._first(record, _COIN_FIELDS)
                amount_raw = self._first(record, _AMOUNT_FIELDS)
                if asset is None or amount_raw is None:
                    continue
                result.add_amount(str(asset), parse_decimal(amount_raw))
            result.raw_records_count += len(records)

            # VERIFIED (live): paginate using the response-level `endId` cursor
            # (records carry no id). Stop on a short/last page, a missing cursor,
            # or a non-advancing cursor (defensive against an infinite loop).
            if len(records) < _PAGE_LIMIT or not end_id or str(end_id) == id_less_than:
                break
            id_less_than = str(end_id)

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT rebate across every customer under this broker key.

        Unlike Gate/MEXC/BitMart (VERIFIED: those commission endpoints return every
        referral's rows when the uid filter is omitted), Bitget's own docs mark
        `uid` on ``customer-commissions`` as "Required: No" but never state what an
        omitted uid actually returns — that shortcut is UNCONFIRMED for Bitget, so
        we do NOT take it here (ASSUMED-uncertain, deliberately unused).

        Instead: list-then-sum. Enumerate the full customer roster via
        ``/api/v2/broker/customer-list`` (the same endpoint :meth:`verify_referral`
        already calls) with NO uid filter, then reuse the exact per-uid
        :meth:`_collect_window` pagination :meth:`get_commission` uses, summing only
        the USDT lines. Honours ``max_window_days``/pagination for every customer.

        # ASSUMED: Bitget's docs say customer-list's startTime/endTime span must be
        # <=30 days when supplied, but say nothing about the default when they are
        # omitted entirely. verify_referral already omits them and (VERIFIED live)
        # finds a matching customer regardless of registerTime, i.e. an unfiltered
        # call already searches the whole roster rather than defaulting to
        # "yesterday only" — we rely on that same observed behaviour here to page
        # through EVERY customer, not just ones who registered inside the query
        # window (a customer who joined long before date_from can still have
        # earned commission inside it).
        """
        self._log.info("get_downline_total", date_from=str(date_from), date_to=str(date_to))
        total = Decimal(0)
        async for uid in self._iter_all_customer_uids():
            temp = self._new_result(uid, date_from, date_to)
            for window_start, window_end in iter_windows(
                date_from.date(), date_to.date(), self.max_window_days
            ):
                await self._collect_window(uid, window_start, window_end, temp)
            for line in temp.finalize().lines:
                if line.asset == "USDT":
                    total += line.amount
        return total

    async def _iter_all_customer_uids(self) -> AsyncIterator[str]:
        """Page through the broker's entire customer roster (all uids, all time).

        POSTs ``customer-list`` with no uid/time filter, paginating via
        ``pageNo``/``pageSize`` (VERIFIED docs: that endpoint's own pagination
        fields — it has no ``idLessThan``/``endId`` cursor like customer-commissions,
        response ``data`` is a flat array of ``{uid, registerTime}``). Stops on a
        short/empty page.
        """
        page_no = 1
        while True:
            body = json.dumps(
                {"pageNo": str(page_no), "pageSize": str(_PAGE_LIMIT)}, separators=(",", ":")
            )
            payload = await self._send(
                "POST",
                _CUSTOMER_LIST_PATH,
                headers=self._headers("POST", _CUSTOMER_LIST_PATH, body),
                log_path=_CUSTOMER_LIST_PATH,
                content=body,
            )
            records, _ = self._extract_page(payload)
            for record in records:
                uid = record.get("uid")
                if uid is not None and str(uid):
                    yield str(uid)
            if len(records) < _PAGE_LIMIT:
                break
            page_no += 1

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """POST the customer list filtered by ``uid`` (no date -> full roster).

        VERIFIED (docs): ``customer-list`` returns the broker's customers (uid +
        registerTime) independent of any rebate. A matching row confirms; an empty
        result is an authoritative non-customer, so absence rejects. Signed over the
        exact JSON body (Bitget POST scheme).
        """
        body = json.dumps({"uid": uid}, separators=(",", ":"))
        payload = await self._send(
            "POST",
            _CUSTOMER_LIST_PATH,
            headers=self._headers("POST", _CUSTOMER_LIST_PATH, body),
            log_path=_CUSTOMER_LIST_PATH,
            content=body,
        )
        records, _ = self._extract_page(payload)
        for record in records:
            if str(record.get("uid")) == uid:
                return ReferralCheck.CONFIRMED
        return ReferralCheck.NOT_REFERRAL

    def _extract_page(self, payload: object) -> tuple[list[dict], str | None]:
        """Return (records, endId cursor) from a Bitget commissions response."""
        if not isinstance(payload, dict):
            return [], None
        if str(payload.get("code", "00000")) != "00000":
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")
        data = payload.get("data")
        if isinstance(data, list):
            return data, None
        if isinstance(data, dict):
            end_id = data.get("endId")
            for key in ("commissionList", "list", "items"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner, (str(end_id) if end_id else None)
        return [], None

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
