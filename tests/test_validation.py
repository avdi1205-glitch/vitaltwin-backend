"""Unit tests for `app.core.validation` (Twin Intelligence Core, Etappe 2).

Pure functions, no database/network access required.
"""

from datetime import date, timedelta

import pytest

from app.core.validation import (
    validate_local_date_not_future,
    validate_long_text,
    validate_movement_minutes,
    validate_scale_1_to_10,
    validate_short_text,
    validate_sleep_hours,
    validate_timezone_name,
)


class TestScale1To10:
    @pytest.mark.parametrize("value", [1, 5, 10])
    def test_accepts_values_in_range(self, value):
        assert validate_scale_1_to_10(value, field_name="Energie") == value

    def test_accepts_none(self):
        assert validate_scale_1_to_10(None, field_name="Energie") is None

    @pytest.mark.parametrize("value", [0, -1, 11, 100])
    def test_rejects_values_out_of_range(self, value):
        with pytest.raises(ValueError):
            validate_scale_1_to_10(value, field_name="Stress")


class TestSleepHours:
    @pytest.mark.parametrize("value", [0, 7.5, 16])
    def test_accepts_valid_range(self, value):
        assert validate_sleep_hours(value) == value

    @pytest.mark.parametrize("value", [-0.1, 16.1, 24])
    def test_rejects_invalid_range(self, value):
        with pytest.raises(ValueError):
            validate_sleep_hours(value)


class TestMovementMinutes:
    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            validate_movement_minutes(-1)

    def test_rejects_more_than_a_day(self):
        with pytest.raises(ValueError):
            validate_movement_minutes(1441)

    def test_accepts_zero_and_full_day(self):
        assert validate_movement_minutes(0) == 0
        assert validate_movement_minutes(1440) == 1440


class TestLocalDateNotFuture:
    def test_rejects_tomorrow(self):
        tomorrow = date.today() + timedelta(days=1)
        with pytest.raises(ValueError):
            validate_local_date_not_future(tomorrow, field_name="Check-in-Datum")

    def test_accepts_today_and_past(self):
        today = date.today()
        assert validate_local_date_not_future(today) == today
        yesterday = today - timedelta(days=1)
        assert validate_local_date_not_future(yesterday) == yesterday


class TestShortAndLongText:
    def test_strips_whitespace(self):
        assert validate_short_text("  hallo  ", field_name="Notiz") == "hallo"

    def test_empty_becomes_none(self):
        assert validate_short_text("   ", field_name="Notiz") is None

    def test_rejects_too_long_short_text(self):
        with pytest.raises(ValueError):
            validate_short_text("x" * 201, field_name="Notiz", max_length=200)

    def test_rejects_too_long_long_text(self):
        with pytest.raises(ValueError):
            validate_long_text("x" * 2001, field_name="Reflexion")

    def test_long_text_allows_up_to_2000(self):
        assert validate_long_text("x" * 2000, field_name="Reflexion") == "x" * 2000


class TestTimezoneName:
    def test_accepts_valid_iana_timezone(self):
        assert validate_timezone_name("Europe/Berlin") == "Europe/Berlin"

    def test_rejects_made_up_timezone(self):
        with pytest.raises(ValueError):
            validate_timezone_name("Not/AZone")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            validate_timezone_name("")
