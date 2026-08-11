"""Unit tests for `app.services.daily_signal_view` (Twin Core Phase 3 —
Cross-Domain Intelligence). Pure alignment over already-fetched rows, no
database access."""

from __future__ import annotations

from datetime import date, timedelta

from app.services.daily_signal_view import build_daily_signals, to_next_day_shifted_rows, to_same_day_rows

TODAY = date(2026, 8, 11)


def checkin(days_ago: int, **fields) -> dict:
    return {"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), **fields}


def google_steps_row(days_ago: int, value: float) -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + "T08:00:00+00:00"
    return {"user_id": 1, "data_type": "steps", "start_time": ts, "value": value}


def cgm_row(days_ago: int, value: float) -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + "T08:00:00+00:00"
    return {"glucose_value": value, "reading_at": ts}


def nutrition_row(days_ago: int, carbs: float) -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + "T12:00:00+00:00"
    return {"carbs": carbs, "logged_at": ts}


EMPTY = dict(checkin_entries=[], google_steps_rows=[], cgm_rows=[], nutrition_rows=[], today=TODAY, window_days=30)


class TestBuildDailySignalsMissingData:
    def test_empty_everything_yields_no_days(self):
        assert build_daily_signals(**EMPTY) == {}

    def test_a_day_with_no_cgm_reading_has_no_glucose_key(self):
        signals = build_daily_signals(**{**EMPTY, "checkin_entries": [checkin(0, sleep_hours=7.5)]})
        assert "glucose_mean" not in signals[TODAY]
        assert signals[TODAY]["sleep_hours"] == 7.5

    def test_missing_day_is_absent_never_zero(self):
        # Only day 0 has any data at all — day 1 must not exist as a key,
        # and must never silently become {"...": 0}.
        signals = build_daily_signals(**{**EMPTY, "cgm_rows": [cgm_row(0, 100)]})
        assert (TODAY - timedelta(days=1)) not in signals


class TestBuildDailySignalsRealData:
    def test_checkin_fields_are_merged_per_day(self):
        signals = build_daily_signals(**{**EMPTY, "checkin_entries": [checkin(0, sleep_hours=7.0, energy=4, stress=2)]})
        assert signals[TODAY] == {"sleep_hours": 7.0, "energy": 4.0, "stress": 2.0}

    def test_google_steps_are_summed_per_day(self):
        rows = [google_steps_row(0, 3000), google_steps_row(0, 2000)]
        signals = build_daily_signals(**{**EMPTY, "google_steps_rows": rows})
        assert signals[TODAY]["google_steps"] == 5000.0

    def test_cgm_is_averaged_per_day(self):
        rows = [cgm_row(0, 100), cgm_row(0, 120)]
        signals = build_daily_signals(**{**EMPTY, "cgm_rows": rows})
        assert signals[TODAY]["glucose_mean"] == 110.0

    def test_nutrition_carbs_are_summed_per_day(self):
        rows = [nutrition_row(0, 40), nutrition_row(0, 30)]
        signals = build_daily_signals(**{**EMPTY, "nutrition_rows": rows})
        assert signals[TODAY]["nutrition_carbs"] == 70.0

    def test_multiple_sources_on_the_same_day_coexist(self):
        signals = build_daily_signals(**{
            **EMPTY,
            "checkin_entries": [checkin(0, sleep_hours=7.0)],
            "google_steps_rows": [google_steps_row(0, 5000)],
            "cgm_rows": [cgm_row(0, 100)],
            "nutrition_rows": [nutrition_row(0, 40)],
        })
        assert signals[TODAY] == {"sleep_hours": 7.0, "google_steps": 5000.0, "glucose_mean": 100.0, "nutrition_carbs": 40.0}

    def test_window_excludes_days_outside_range(self):
        signals = build_daily_signals(**{**EMPTY, "cgm_rows": [cgm_row(40, 100)], "window_days": 30})
        assert signals == {}


class TestSameDayRows:
    def test_converts_dict_to_list_of_rows(self):
        daily_signals = {TODAY: {"google_steps": 5000.0, "glucose_mean": 100.0}}
        rows = to_same_day_rows(daily_signals)
        assert rows == [{"entry_date": TODAY.isoformat(), "google_steps": 5000.0, "glucose_mean": 100.0}]


class TestNextDayShiftedRows:
    def test_pairs_day_n_field_with_day_n_plus_1_field(self):
        day0 = TODAY - timedelta(days=1)
        day1 = TODAY
        daily_signals = {day0: {"sleep_hours": 6.0}, day1: {"energy": 3.0}}
        rows = to_next_day_shifted_rows(daily_signals, day_field="sleep_hours", next_day_field="energy")
        assert rows == [{"entry_date": day0.isoformat(), "sleep_hours": 6.0, "energy": 3.0}]

    def test_no_next_day_data_excludes_the_row(self):
        daily_signals = {TODAY: {"sleep_hours": 6.0}}  # no day+1 entry at all
        rows = to_next_day_shifted_rows(daily_signals, day_field="sleep_hours", next_day_field="energy")
        assert rows == []

    def test_next_day_present_but_missing_the_target_field_excludes_the_row(self):
        day0 = TODAY - timedelta(days=1)
        daily_signals = {day0: {"sleep_hours": 6.0}, TODAY: {"stress": 5.0}}  # no "energy" on day+1
        rows = to_next_day_shifted_rows(daily_signals, day_field="sleep_hours", next_day_field="energy")
        assert rows == []
