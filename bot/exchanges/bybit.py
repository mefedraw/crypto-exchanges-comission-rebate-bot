"""Bybit affiliate commission adapter (arbitrary date range by uid).

Endpoint: GET /v5/affiliate/aff-user-list
Docs: https://bybit-exchange.github.io/docs/v5/affiliate/affiliate-user-list

VERIFIED (live): with ``startDate``/``endDate`` (``YYYY-MM-DD``) the response carries
``commissionsVol`` — the per-coin commission for exactly that window, distinct from the
rolling ``commissions30Day`` / ``commissions365Day`` figures. The endpoint has **no**
server-side uid filter, so we page through the referral list (``size`` ≤ 100, follow
``nextPageCursor``) and match ``userId`` client-side. API key needs Affiliate permission
only.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex
from bot.utils.dates import DATE_FORMAT
from bot.utils.money import parse_decimal

_PATH = "/v5/affiliate/aff-user-list"
# VERIFIED (live): per-uid membership endpoint. retCode 0 => the uid IS the
# affiliate's referral; retCode 141024 "no relation with affiliate" => it is not.
# Authoritative and amount-independent, so verify_referral hard-rejects on 141024.
_AFF_CUSTOMER_INFO_PATH = "/v5/user/aff-customer-info"
_TICKERS_PATH = "/v5/market/tickers"
_NO_RELATION_CODE = "141024"
_RECV_WINDOW = "5000"
_PAGE_SIZE = 100  # VERIFIED: aff-user-list caps `size` at [0, 100].
# Defensive cap: at 100 referrals/page this scans up to 20 000 referrals before
# giving up. Prevents an unbounded loop if `nextPageCursor` never clears.
_MAX_PAGES = 200


class BybitAdapter(BaseHttpAdapter):
    name = "Bybit"
    code = "bybit"
    base_url = "https://api.bybit.com"  # VERIFIED: Bybit v5 REST host.
    max_window_days = 365
    supports_uid_filter = True  # filtered client-side (API has no uid param).
    supports_date_range = True  # VERIFIED (live): commissionsVol per [startDate, endDate].

    def _headers(self, query: str) -> dict[str, str]:
        """Bybit v5 signing: SIGN = hmac_sha256_hex(ts + apiKey + recvWindow + query)."""
        # VERIFIED: GET signature scheme per Bybit v5 auth docs.
        timestamp = str(int(time.time() * 1000))
        api_key = self._creds.api_key.get_secret_value()
        payload = f"{timestamp}{api_key}{_RECV_WINDOW}{query}"
        sign = hmac_hex(self._creds.api_secret.get_secret_value(), payload)
        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        # Bybit takes plain calendar dates (UTC+8 in the partner cabinet), not
        # timestamps, so the requested range goes through verbatim — no windowing.
        # ASSUMED: dates are interpreted in Bybit's UTC+8 affiliate day, matching the
        # cabinet and the other exchanges in this repo.
        start_date = date_from.date().strftime(DATE_FORMAT)
        end_date = date_to.date().strftime(DATE_FORMAT)

        user = await self._find_user(uid, start_date, end_date, result)
        if user is None:
            result.notes.append(
                "uid не найден в списке рефералов Bybit за этот период "
                "(возможно, реферал не привязан к этому аккаунту или не торговал)."
            )
            result.settlement_note = (
                "Bybit: сверяйтесь с Affiliate Portal — API не источник истины для выплат."
            )
            return result.finalize()

        result.raw_records_count = 1
        # VERIFIED (live): commissionsVol is a {coin: amount} map for [startDate, endDate].
        self._add_usdt_commission(result, user.get("commissionsVol"))
        result.settlement_note = (
            "Сумма начислений Bybit за период (по данным API). Точные выплаты — в Affiliate Portal."
        )
        return result.finalize()

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Sum USDT ``commissionsVol`` across the ENTIRE affiliate downline.

        Reuses the same ``aff-user-list`` pagination as :meth:`get_commission`'s
        ``_find_user``, but instead of matching a single ``userId`` it walks every
        page and adds every row's USDT commission — including referrals who never
        registered in the bot, per :meth:`ExchangeAdapter.get_downline_total`.

        # ASSUMED: aggregating aff-user-list across ALL rows (rather than the
        # single-uid lookup used elsewhere in this file) has not been separately
        # live-verified — only the per-row ``commissionsVol`` shape and pagination
        # mechanics were confirmed live for get_commission. Needs a live check with
        # a real affiliate key that has more than one referral before shipping.
        """
        start_date = date_from.date().strftime(DATE_FORMAT)
        end_date = date_to.date().strftime(DATE_FORMAT)
        self._log.info("get_downline_total", date_from=str(date_from), date_to=str(date_to))

        total = Decimal(0)
        cursor = ""
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {
                "size": str(_PAGE_SIZE),
                "startDate": start_date,
                "endDate": end_date,
            }
            if cursor:
                params["cursor"] = cursor
            query = urlencode(params)
            payload = await self._send(
                "GET", f"{_PATH}?{query}", headers=self._headers(query), log_path=_PATH
            )
            data = self._extract_result(payload)
            rows = data.get("list")
            rows = rows if isinstance(rows, list) else []
            for row in rows:
                if isinstance(row, dict):
                    total += self._extract_usdt(row.get("commissionsVol"))

            cursor = str(data.get("nextPageCursor") or "")
            if not cursor or not rows:
                return total

        self._log.warning("aff-user-list downline page cap reached", pages=_MAX_PAGES)
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Ask Bybit directly whether ``uid`` is the affiliate's referral.

        VERIFIED (live), all HTTP 200: retCode 0 -> CONFIRMED; retCode 141024
        "no relation with affiliate" -> NOT_REFERRAL (authoritative,
        amount-independent); anything else -> UNVERIFIABLE (fail open). Classifies
        retCode directly (``_extract_result`` would raise on 141024 and bury it).
        """
        query = urlencode({"uid": uid})
        payload = await self._send(
            "GET",
            f"{_AFF_CUSTOMER_INFO_PATH}?{query}",
            headers=self._headers(query),
            log_path=_AFF_CUSTOMER_INFO_PATH,
        )
        if not isinstance(payload, dict):
            return ReferralCheck.UNVERIFIABLE
        code = str(payload.get("retCode"))
        if code == "0":
            return ReferralCheck.CONFIRMED
        if code == _NO_RELATION_CODE:
            return ReferralCheck.NOT_REFERRAL
        return ReferralCheck.UNVERIFIABLE

    async def mnt_usdt_rate(self) -> Decimal:
        """Public spot MNTUSDT last price (no auth). VERIFIED: Bybit v5 market/tickers."""
        query = urlencode({"category": "spot", "symbol": "MNTUSDT"})
        payload = await self._send(
            "GET",
            f"{_TICKERS_PATH}?{query}",
            headers={"Accept": "application/json"},
            log_path=_TICKERS_PATH,
        )
        result = self._extract_result(payload)
        rows = result.get("list")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            price = rows[0].get("lastPrice")
            if isinstance(price, str | int | float | Decimal) and price != "":
                return parse_decimal(price)
        raise ExchangeApiError(f"{self.name}: no MNTUSDT price")

    async def _find_user(
        self, uid: str, start_date: str, end_date: str, result: CommissionResult
    ) -> dict[str, object] | None:
        """Page through aff-user-list looking for ``uid``; return its row or None."""
        cursor = ""
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {
                "size": str(_PAGE_SIZE),
                "startDate": start_date,
                "endDate": end_date,
            }
            if cursor:
                params["cursor"] = cursor
            query = urlencode(params)
            payload = await self._send(
                "GET", f"{_PATH}?{query}", headers=self._headers(query), log_path=_PATH
            )
            data = self._extract_result(payload)
            rows = data.get("list")
            rows = rows if isinstance(rows, list) else []
            for row in rows:
                if isinstance(row, dict) and str(row.get("userId")) == uid:
                    return row

            cursor = str(data.get("nextPageCursor") or "")
            if not cursor or not rows:
                return None

        self._log.warning("aff-user-list page cap reached", uid=uid, pages=_MAX_PAGES)
        result.notes.append(
            f"Просмотрены первые {_MAX_PAGES * _PAGE_SIZE} рефералов — uid не найден среди них."
        )
        return None

    def _extract_result(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            return {}
        if payload.get("retCode") not in (0, "0", None):
            raise ExchangeApiError(f"{self.name}: {payload.get('retCode')} {payload.get('retMsg')}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def _add_usdt_commission(self, result: CommissionResult, value: object) -> None:
        """Record USDT and MNT commission as separate lines. Other coins (USDC, ...)
        are intentionally ignored — by owner's requirement Bybit shows only USDT/MNT.

        VERIFIED (live): commissionsVol is a ``{coin: amount}`` map; coins with no
        commission carry ``"0"`` (or an empty string), e.g.
        ``{"BTC":"0", "MNT":"0", "USDT":"0.83421325"}``. A list of ``{coin, amount}``
        rows is also accepted defensively.
        """
        other_coins_present = False

        if isinstance(value, dict):
            for coin, amount in value.items():
                if self._is_reported(coin):
                    amt = parse_decimal(amount or 0)  # empty string => 0
                    if amt != Decimal(0):
                        result.add_amount(str(coin).upper(), amt)
                elif self._nonzero(amount):
                    other_coins_present = True
        elif isinstance(value, list):
            # ASSUMED (verify live): [{coin/currency, amount/commission}] rows.
            for row in value:
                if not isinstance(row, dict):
                    continue
                coin = row.get("coin") or row.get("coinName") or row.get("currency")
                amount = row.get("amount") or row.get("commission") or row.get("value")
                if self._is_reported(coin):
                    amt = parse_decimal(amount or 0)
                    if amt != Decimal(0):
                        result.add_amount(str(coin).upper(), amt)
                elif self._nonzero(amount):
                    other_coins_present = True
        elif value is not None:
            # Scalar: no per-coin split available. Best effort: treat as USDT.
            usdt = parse_decimal(value)
            if usdt != Decimal(0):
                result.add_amount("USDT", usdt)

        if other_coins_present:
            result.notes.append(
                "Показаны только USDT и MNT; начисления в других монетах не учтены."
            )

    @staticmethod
    def _extract_usdt(value: object) -> Decimal:
        """Pull just the USDT amount out of one row's ``commissionsVol``.

        # ASSUMED: same per-row shape as ``_add_usdt_commission`` (dict keyed by
        # coin, a list of ``{coin, amount}`` rows, or a bare scalar) — the dict
        # shape is the one confirmed live; the others are defensive fallbacks.
        """
        if isinstance(value, dict):
            for coin, amount in value.items():
                if str(coin).upper() == "USDT":
                    return parse_decimal(amount or 0)
            return Decimal(0)
        if isinstance(value, list):
            for row in value:
                if not isinstance(row, dict):
                    continue
                coin = row.get("coin") or row.get("coinName") or row.get("currency")
                if str(coin).upper() == "USDT":
                    amount = row.get("amount") or row.get("commission") or row.get("value")
                    return parse_decimal(amount or 0)
            return Decimal(0)
        if value is not None:
            return parse_decimal(str(value))
        return Decimal(0)

    @staticmethod
    def _is_reported(coin: object) -> bool:
        return str(coin).upper() in ("USDT", "MNT")

    @staticmethod
    def _nonzero(amount: object) -> bool:
        try:
            return parse_decimal(amount if amount is not None else 0) != Decimal(0)
        except Exception:  # defensive; unknown shapes shouldn't crash
            return False


@register("bybit")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BybitAdapter:
    return BybitAdapter(credentials, settings)
