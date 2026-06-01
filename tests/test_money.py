from __future__ import annotations

from decimal import Decimal

import pytest

from bot.utils.money import MoneyError, format_amount, parse_decimal, sum_amounts


def test_parse_decimal_from_string_and_number():
    assert parse_decimal("12.34") == Decimal("12.34")
    assert parse_decimal(5) == Decimal("5")
    assert parse_decimal(Decimal("1.0")) == Decimal("1.0")


def test_parse_decimal_float_avoids_binary_error():
    # 0.1 as float is imprecise; we stringify first so it parses cleanly.
    assert parse_decimal(0.1) == Decimal("0.1")


def test_parse_decimal_rejects_garbage():
    with pytest.raises(MoneyError):
        parse_decimal("abc")


def test_sum_amounts_exact():
    assert sum_amounts([Decimal("0.1"), Decimal("0.2")]) == Decimal("0.3")
    assert sum_amounts([]) == Decimal(0)


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("12.4500"), "12.45"),
        (Decimal("0E-8"), "0"),
        (Decimal("0"), "0"),
        (Decimal("100"), "100"),
        (Decimal("0.000052"), "0.000052"),
    ],
)
def test_format_amount(value, expected):
    assert format_amount(value) == expected
