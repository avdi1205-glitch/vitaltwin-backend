"""Unit tests for `app.services.google_health_signals` (Twin Core Phase 1
— Google Health -> Twin Intelligence). Pure aggregation over already-fetched
rows, no database access — mirrors the testing style of
test_trends.py/test_personal_baseline.py."""

from __future__ import annotations

from datetime import date, timedelta

from app.services import google_health_signals as ghs

TODAY = date(2026, 8, 11)


def activity_row(days_ago: int, value: float, *, data_type: str = "steps") -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + "T08:00:00+00:00"
    return {"user_id": 1, "data_type": data_type, "start_time": ts, "value": value}


def metric_row(days_ago: int, value: float, *, data_type: str = "weight") -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + "T08:00:00+00:00"
    return {"user_id": 1, "data_type": data_type, "observed_at": ts, "value": value}


def sleep_row(days_ago: int, seconds: float) -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + "T22:00:00+00:00"
    return {"user_id": 1, "start_time": ts, "duration_seconds": seconds}


def manual_entry(days_ago: int, **fields) -> dict:
    return {"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), **fields}


class TestBuildSignalNoData:
    def test_empty_rows_yield_no_data(self):
        signal = ghs.build_signal([], signal="steps", today=TODAY)
        assert signal.has_data is False
        assert signal.data_points == 0
        assert signal.trend.average is None
        assert signal.trend.data_quality == "missing"


class TestBuildSignalRealSteps:
    def test_multiple_daily_records_are_summed_per_day_then_averaged(self):
        # 2 records on the same day (e.g. two interval buckets) must SUM to
        # a daily total, not be averaged as if they were 2 separate days.
        rows = [
            activity_row(0, 3000),
            activity_row(0, 2000),  # same day as above -> 5000 total
            activity_row(1, 4000),
        ]
        signal = ghs.build_signal(rows, signal="steps", today=TODAY)
        assert signal.has_data is True
        assert signal.data_points == 3
        # (5000 + 4000) / 2 days = 4500
        assert signal.trend.average == 4500.0
        assert signal.unit == "Schritte"

    def test_latest_value_reflects_most_recent_raw_record(self):
        rows = [activity_row(3, 1000), activity_row(0, 9999)]
        signal = ghs.build_signal(rows, signal="steps", today=TODAY)
        assert signal.latest_value == 9999.0


class TestBuildSignalRealWeight:
    def test_same_day_samples_are_averaged_not_summed(self):
        rows = [metric_row(0, 80.0), metric_row(0, 82.0)]
        signal = ghs.build_signal(rows, signal="weight", today=TODAY)
        assert signal.trend.average == 81.0
        assert signal.unit == "kg"


class TestBuildSignalRealSleepAndActiveMinutes:
    def test_sleep_duration_sums_segments_per_night(self):
        rows = [sleep_row(0, 3600 * 3), sleep_row(0, 3600 * 4)]
        signal = ghs.build_signal(rows, signal="sleep_duration", today=TODAY)
        assert signal.trend.average == 3600 * 7

    def test_active_minutes_real_data(self):
        rows = [activity_row(i, 45, data_type="active-minutes") for i in range(5)]
        signal = ghs.build_signal(rows, signal="active_minutes", today=TODAY)
        assert signal.has_data is True
        assert signal.trend.data_quality == "calculated"


class TestBuildBaseline:
    def test_insufficient_baseline_history_returns_not_available(self):
        # Only recent-window data exists — no baseline-window data at all.
        rows = [activity_row(i, 8000) for i in range(7)]
        baseline = ghs.build_baseline(rows, signal="steps", today=TODAY)
        assert baseline.available is False
        assert "Noch nicht genügend" in baseline.message

    def test_valid_personal_baseline_uses_non_overlapping_windows(self):
        recent = [activity_row(i, 8960) for i in range(7)]
        baseline_window = [activity_row(i, 8000) for i in range(8, 28)]
        rows = recent + baseline_window
        baseline = ghs.build_baseline(rows, signal="steps", today=TODAY)
        assert baseline.available is True
        assert baseline.recent_average == 8960.0
        assert baseline.baseline_average == 8000.0


class TestResolveTrendSource:
    def test_google_data_present_takes_precedence_over_manual(self):
        google_rows = [activity_row(i, 9000) for i in range(3)]
        manual_entries = [manual_entry(i, steps=1000) for i in range(3)]
        resolved = ghs.resolve_trend_source(
            signal="steps", google_rows=google_rows, manual_entries=manual_entries, today=TODAY
        )
        assert resolved.source == ghs.SOURCE_GOOGLE_HEALTH
        assert resolved.trend.average == 9000.0

    def test_falls_back_to_manual_when_no_google_data(self):
        manual_entries = [manual_entry(i, steps=5000) for i in range(3)]
        resolved = ghs.resolve_trend_source(
            signal="steps", google_rows=[], manual_entries=manual_entries, today=TODAY
        )
        assert resolved.source == ghs.SOURCE_MANUAL_CHECKIN
        assert resolved.trend.average == 5000.0

    def test_neither_source_has_data(self):
        resolved = ghs.resolve_trend_source(signal="steps", google_rows=[], manual_entries=[], today=TODAY)
        assert resolved.source == ghs.SOURCE_NONE
        assert resolved.trend.average is None

    def test_sleep_duration_precedence_uses_sleep_hours_manual_field(self):
        manual_entries = [manual_entry(i, sleep_hours=7.0) for i in range(3)]
        resolved = ghs.resolve_trend_source(
            signal="sleep_duration", google_rows=[], manual_entries=manual_entries, today=TODAY
        )
        assert resolved.source == ghs.SOURCE_MANUAL_CHECKIN
        assert resolved.trend.average == 7.0


class TestSignalToContextDict:
    def test_google_health_signal_shape(self):
        signal = ghs.build_signal([activity_row(0, 5000)], signal="steps", today=TODAY)
        result = ghs.signal_to_context_dict(signal)
        assert result["has_data"] is True
        assert result["source"] == ghs.SOURCE_GOOGLE_HEALTH
        assert result["unit"] == "Schritte"
        assert result["average"] == 5000.0

    def test_no_data_signal_has_none_source(self):
        signal = ghs.build_signal([], signal="steps", today=TODAY)
        result = ghs.signal_to_context_dict(signal)
        assert result["has_data"] is False
        assert result["source"] == ghs.SOURCE_NONE

    def test_resolved_trend_shape_carries_its_own_source(self):
        resolved = ghs.resolve_trend_source(
            signal="steps",
            google_rows=[],
            manual_entries=[manual_entry(0, steps=1234)],
            today=TODAY,
        )
        result = ghs.signal_to_context_dict(resolved, unit="Schritte")
        assert result["source"] == ghs.SOURCE_MANUAL_CHECKIN
        assert result["unit"] == "Schritte"
        assert result["average"] == 1234.0
