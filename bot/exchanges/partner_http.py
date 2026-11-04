"""Strict response parsing shared by the newer partner adapters.

Malformed/partial financial responses must raise, never become a frozen zero.
Only schema field names and bounded error codes appear in exceptions, not bodies.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import ExchangeApiError
from bot.exchanges.base_http import BaseHttpAdapter
from bot.utils.dates import iter_windows


def object_of(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExchangeApiError("Partner API: expected an object")
    return cast(dict[str, Any], value)


def rows_of(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ExchangeApiError("Partner API: expected a record list")
    return [object_of(row) for row in value]


def amount_of(value: object) -> Decimal:
    # No float round-trip, bool, missing value, NaN or infinity in the money path.
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise ExchangeApiError("Partner API: missing or invalid commission amount")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ExchangeApiError("Partner API: invalid commission amount") from exc
    if not amount.is_finite():
        raise ExchangeApiError("Partner API: non-finite commission amount")
    return amount


def integer_of(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ExchangeApiError("Partner API: invalid count/timestamp")
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,20}", text):
        raise ExchangeApiError("Partner API: invalid count/timestamp")
    return int(text)


def numeric_uid(uid: str) -> str:
    uid = uid.strip()
    if not re.fullmatch(r"[1-9][0-9]{0,31}", uid):
        raise ExchangeApiError("UID должен быть положительным числовым идентификатором.")
    return uid


def asset_of(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{2,16}", value.strip()):
        raise ExchangeApiError("Partner API: missing or invalid commission currency")
    return value.strip().upper()


def calendar_windows(
    date_from: datetime, date_to: datetime, days: int
) -> Iterator[tuple[datetime, datetime]]:
    # Follow the existing worker's calendar-label convention. Date-only APIs
    # cannot express an intraday interval; the adapter's settlement note names its day.
    if date_to < date_from:
        raise ExchangeApiError("Дата конца раньше даты начала.")
    if date_from.utcoffset() is None or date_to.utcoffset() is None:
        raise ExchangeApiError("Partner API: timezone-aware dates required")
    return iter_windows(date_from.date(), date_to.date(), days)


def instant_windows(
    date_from: datetime, date_to: datetime, days: int, *, unit: timedelta
) -> Iterator[tuple[datetime, datetime]]:
    """Inclusive absolute windows, without losing the worker's UTC+8 offset."""
    if date_from.utcoffset() is None or date_to.utcoffset() is None:
        raise ExchangeApiError("Partner API: timezone-aware dates required")
    if (
        date_to < date_from
        or days < 1
        or unit not in (timedelta(seconds=1), timedelta(milliseconds=1))
    ):
        raise ExchangeApiError("Partner API: invalid time window")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    start = epoch + ((date_from.astimezone(UTC) - epoch) // unit) * unit
    stop = epoch + ((date_to.astimezone(UTC) - epoch) // unit) * unit
    while start <= stop:
        end = min(stop, start + timedelta(days=days) - unit)
        yield start, end
        start = end + unit


def safe_code(value: object) -> str:
    code = str(value)
    return code if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code) else "unknown"


class PartnerHttpAdapter(BaseHttpAdapter):
    """Decimal JSON, bounded request pacing and secret-free HTTP failures."""

    request_interval: float = 0.0
    # ASSUMED: one additional closed day is an operational settlement buffer;
    # it is not a claim that the exchange guarantees finality after one day.
    settlement_lag_days = 1

    def __init__(self, credentials: ExchangeCredentials, settings: Settings) -> None:
        super().__init__(credentials, settings)
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    def _decode_response(self, response: httpx.Response) -> Any:
        # Preserve numeric commission fields exactly, even if returned unquoted.
        return response.json(parse_float=Decimal)

    async def _send(
        self,
        method: str,
        request_path: str,
        *,
        headers: dict[str, str] | None = None,
        log_path: str | None = None,
        content: str | None = None,
    ) -> Any:
        async with self._request_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = time.monotonic() + self.request_interval
            return await super()._send(
                method, request_path, headers=headers, log_path=log_path, content=content
            )

    def _check_response(self, payload: object) -> dict[str, Any]:
        raise NotImplementedError

    def _raise_client_error(self, status: int, response: httpx.Response) -> None:
        payload = self._safe_json(response)
        if payload is not None:
            self._check_response(payload)
        raise ExchangeApiError(f"{self.name}: HTTP {status}")
