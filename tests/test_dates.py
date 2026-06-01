from __future__ import annotations

from datetime import date

import pytest

from bot.utils.dates import (
    DateInputError,
    day_end,
    day_start,
    format_display,
    iter_windows,
    parse_date,
    parse_ddmmyyyy,
    parse_period,
    smart_month,
    to_millis,
    to_seconds,
    validate_period,
)


def test_parse_date_ok():
    assert parse_date(" 2026-05-01 ") == date(2026, 5, 1)


@pytest.mark.parametrize("bad", ["2026-13-01", "01-05-2026", "2026/05/01", "", "nope"])
def test_parse_date_rejects_bad(bad):
    with pytest.raises(DateInputError):
        parse_date(bad)


def test_validate_period_ok():
    validate_period(date(2026, 1, 1), date(2026, 1, 31), today=date(2026, 6, 1))


def test_validate_period_end_before_start():
    with pytest.raises(DateInputError):
        validate_period(date(2026, 2, 1), date(2026, 1, 1), today=date(2026, 6, 1))


def test_validate_period_in_future():
    with pytest.raises(DateInputError):
        validate_period(date(2026, 7, 1), date(2026, 7, 2), today=date(2026, 6, 1))


def test_validate_period_too_long():
    with pytest.raises(DateInputError):
        validate_period(date(2024, 1, 1), date(2026, 1, 1), today=date(2026, 6, 1))


@pytest.mark.parametrize(
    "start,end,window,expected",
    [
        ("2026-01-01", "2026-01-01", 30, 1),  # single day
        ("2026-01-01", "2026-01-30", 30, 1),  # exactly 30
        ("2026-01-01", "2026-01-31", 30, 2),  # 31 -> 30 + 1
        ("2026-03-01", "2026-04-29", 30, 2),  # 60 -> 30 + 30
    ],
)
def test_iter_windows_boundaries(start, end, window, expected):
    windows = list(iter_windows(date.fromisoformat(start), date.fromisoformat(end), window))
    assert len(windows) == expected
    # Windows are contiguous and cover the whole inclusive range.
    assert windows[0][0] == day_start(date.fromisoformat(start))
    assert windows[-1][1] == day_end(date.fromisoformat(end))


def test_iter_windows_rejects_zero_window():
    with pytest.raises(ValueError):
        list(iter_windows(date(2026, 1, 1), date(2026, 1, 2), 0))


def test_day_bounds_are_utc_and_inclusive():
    start = day_start(date(2026, 5, 1))
    end = day_end(date(2026, 5, 1))
    assert start.isoformat() == "2026-05-01T00:00:00+00:00"
    assert end.hour == 23 and end.minute == 59 and end.second == 59


def test_timestamp_helpers():
    dt = day_start(date(1970, 1, 1))
    assert to_seconds(dt) == 0
    assert to_millis(dt) == 0


def test_parse_ddmmyyyy():
    assert parse_ddmmyyyy("01.05.2026") == date(2026, 5, 1)
    with pytest.raises(DateInputError):
        parse_ddmmyyyy("2026-05-01")


@pytest.mark.parametrize(
    "text",
    ["01.05.2026-31.05.2026", "01.05.2026 - 31.05.2026", "01.05.2026—31.05.2026"],
)
def test_parse_period_ok(text):
    assert parse_period(text) == (date(2026, 5, 1), date(2026, 5, 31))


@pytest.mark.parametrize("bad", ["01.05.2026", "a-b", "01.05.2026-", "1.5.26-2.5.26x"])
def test_parse_period_rejects(bad):
    with pytest.raises(DateInputError):
        parse_period(bad)


def test_format_display():
    assert format_display(date(2026, 5, 1)) == "01.05.2026"


def test_smart_month_uses_previous_month_midmonth():
    # Mid-month -> previous (completed) month.
    assert smart_month(date(2026, 6, 15)) == (date(2026, 5, 1), date(2026, 5, 31))


def test_smart_month_uses_current_month_near_end():
    # Day >= 28 -> current month.
    assert smart_month(date(2026, 6, 29)) == (date(2026, 6, 1), date(2026, 6, 30))


def test_smart_month_january_rolls_to_december():
    assert smart_month(date(2026, 1, 10)) == (date(2025, 12, 1), date(2025, 12, 31))
