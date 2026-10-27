from datetime import UTC, date, datetime

import pytest

from bot.payout.expiry import LIFETIME_DAYS, available_at, expires_at, next_withdrawal_allowed


def test_available_at_is_start_of_next_day_utc8():
    # 2026-07-05 rebate becomes available at 2026-07-06 00:00 UTC+8 = 2026-07-05 16:00 UTC.
    assert available_at(date(2026, 7, 5)) == datetime(2026, 7, 5, 16, 0, tzinfo=UTC)


def test_expires_at_is_start_of_day_d_plus_30_utc8():
    # 2026-07-05 + 30 days = 2026-08-04 00:00 UTC+8 = 2026-08-03 16:00 UTC.
    assert expires_at(date(2026, 7, 5)) == datetime(2026, 8, 3, 16, 0, tzinfo=UTC)


def test_expires_at_applies_grace():
    # +2 grace days => 2026-08-06 00:00 UTC+8 = 2026-08-05 16:00 UTC.
    assert expires_at(date(2026, 7, 5), grace_days=2) == datetime(2026, 8, 5, 16, 0, tzinfo=UTC)


def test_lifetime_is_30():
    assert LIFETIME_DAYS == 30


def test_cooldown_from_last_success():
    last = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert next_withdrawal_allowed(last, 3) == datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    assert next_withdrawal_allowed(last, 1) == datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def test_cooldown_none_when_no_history():
    assert next_withdrawal_allowed(None, 2) is None


def test_cooldown_rejects_rank_zero():
    with pytest.raises(ValueError):
        next_withdrawal_allowed(datetime(2026, 7, 1, tzinfo=UTC), 0)
