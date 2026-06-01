"""Date parsing/validation and slicing a period into per-exchange windows.

Conventions:
* User input is ``YYYY-MM-DD`` (a calendar date), interpreted in **UTC**.
* A period ``[date_from, date_to]`` is inclusive of both endpoints.
* ``date_from`` maps to 00:00:00.000000 UTC; ``date_to`` maps to the last
  microsecond of that day, so a single-day period still covers a full day.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone

DATE_FORMAT = "%Y-%m-%d"
DISPLAY_FORMAT = "%d.%m.%Y"  # user-facing DD.MM.YYYY
_MAX_PERIOD_DAYS = 365
# Day-of-month threshold at/after which "smart month" means the current month.
_CURRENT_MONTH_FROM_DAY = 28
# Period separator: hyphen or en/em dash, optional surrounding spaces.
_PERIOD_SEP = re.compile(r"\s*[-–—]\s*")


class DateInputError(ValueError):
    """Raised when user-supplied date input is malformed or out of range."""


def parse_date(raw: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` string into a :class:`date`."""
    text = raw.strip()
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError as exc:
        raise DateInputError("Дата должна быть в формате ГГГГ-ММ-ДД, например 2026-05-01.") from exc


def parse_ddmmyyyy(raw: str) -> date:
    """Parse a strict ``DD.MM.YYYY`` string into a :class:`date`."""
    try:
        return datetime.strptime(raw.strip(), DISPLAY_FORMAT).date()
    except ValueError as exc:
        raise DateInputError("Дата в формате ДД.ММ.ГГГГ, например 01.05.2026.") from exc


def parse_period(raw: str) -> tuple[date, date]:
    """Parse a ``DD.MM.YYYY-DD.MM.YYYY`` range into ``(date_from, date_to)``."""
    parts = _PERIOD_SEP.split(raw.strip())
    if len(parts) != 2 or not all(parts):
        raise DateInputError(
            "Формат периода: ДД.ММ.ГГГГ-ДД.ММ.ГГГГ, например 01.05.2026-31.05.2026."
        )
    return parse_ddmmyyyy(parts[0]), parse_ddmmyyyy(parts[1])


def format_display(d: date) -> str:
    """Format a date as user-facing ``DD.MM.YYYY``."""
    return d.strftime(DISPLAY_FORMAT)


def smart_month(today: date) -> tuple[date, date]:
    """First/last day of the target month for the "calculate the month" preset.

    Near month-end (day >= 28) the current month is assumed to be the one of
    interest; otherwise the previous (completed) month is used.
    """
    if today.day >= _CURRENT_MONTH_FROM_DAY:
        year, month = today.year, today.month
    elif today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def validate_period(date_from: date, date_to: date, *, today: date | None = None) -> None:
    """Validate ordering, the not-in-future rule, and a sane maximum span."""
    today = today or datetime.now(timezone.utc).date()
    if date_to < date_from:
        raise DateInputError("Дата конца не может быть раньше даты начала.")
    if date_from > today:
        raise DateInputError("Период не может начинаться в будущем.")
    if (date_to - date_from).days + 1 > _MAX_PERIOD_DAYS:
        raise DateInputError(f"Период не должен превышать {_MAX_PERIOD_DAYS} дней.")


def day_start(d: date) -> datetime:
    """First instant of the day, in UTC."""
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def day_end(d: date) -> datetime:
    """Last instant of the day (23:59:59.999999), in UTC."""
    return datetime.combine(d, time.max, tzinfo=timezone.utc)


def iter_windows(
    date_from: date, date_to: date, max_window_days: int
) -> Iterator[tuple[datetime, datetime]]:
    """Yield inclusive ``(start, end)`` UTC datetimes, each spanning ≤ window days.

    A 30-day window over a 31-day period yields two chunks (30 days + 1 day).
    """
    if max_window_days < 1:
        raise ValueError("max_window_days must be >= 1")

    cursor = date_from
    while cursor <= date_to:
        chunk_end = min(cursor + timedelta(days=max_window_days - 1), date_to)
        yield day_start(cursor), day_end(chunk_end)
        cursor = chunk_end + timedelta(days=1)


def to_millis(dt: datetime) -> int:
    """Unix epoch milliseconds (exchanges that want ms timestamps)."""
    return int(dt.timestamp() * 1000)


def to_seconds(dt: datetime) -> int:
    """Unix epoch seconds (exchanges that want second timestamps)."""
    return int(dt.timestamp())
