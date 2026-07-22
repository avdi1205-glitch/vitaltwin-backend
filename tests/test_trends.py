"""Unit tests for `app.services.trends` (Sleep/Movement/Stress loops,
Etappe 3). Pure aggregation over already-fetched rows, no database access.
"""

from datetime import date, timedelta

from app.services.trends import compute_trend

TODAY = date(2026, 7, 22)


def entry(days_ago: int, **fields) -> dict:
    return {"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), **fields}


class TestComputeTrend:
    def test_no_entries_yields_missing_quality(self):
        trend = compute_trend([], field="sleep_hours", window_days=7, today=TODAY)
        assert trend.average is None
        assert trend.data_points == 0
        assert trend.data_quality == "missing"

    def test_averages_values_within_window(self):
        entries = [entry(0, sleep_hours=8), entry(1, sleep_hours=6), entry(2, sleep_hours=7)]
        trend = compute_trend(entries, field="sleep_hours", window_days=7, today=TODAY)
        assert trend.average == 7.0
        assert trend.data_points == 3

    def test_ignores_entries_outside_window(self):
        entries = [entry(0, sleep_hours=8), entry(100, sleep_hours=1)]
        trend = compute_trend(entries, field="sleep_hours", window_days=7, today=TODAY)
        assert trend.average == 8.0
        assert trend.data_points == 1

    def test_ignores_entries_missing_the_field(self):
        entries = [entry(0, sleep_hours=8), entry(1, stress_level=3)]
        trend = compute_trend(entries, field="sleep_hours", window_days=7, today=TODAY)
        assert trend.data_points == 1

    def test_few_points_are_marked_partial_not_calculated(self):
        entries = [entry(0, sleep_hours=8)]
        trend = compute_trend(entries, field="sleep_hours", window_days=30, today=TODAY)
        assert trend.data_quality == "partial"

    def test_enough_points_are_marked_calculated(self):
        entries = [entry(i, sleep_hours=7) for i in range(5)]
        trend = compute_trend(entries, field="sleep_hours", window_days=30, today=TODAY)
        assert trend.data_quality == "calculated"

    def test_never_invents_a_value_for_missing_field_entries(self):
        entries = [entry(0), entry(1)]  # neither has sleep_hours at all
        trend = compute_trend(entries, field="sleep_hours", window_days=7, today=TODAY)
        assert trend.average is None
        assert trend.data_points == 0
