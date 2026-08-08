"""Unit tests for `app.services.personal_baseline` (VitalTwin Mehrwert
Phase 1 — Personal Baseline Engine). Pure aggregation over already-fetched
rows, no database access — mirrors the testing style of test_trends.py.
"""

from datetime import date, timedelta

from app.services.personal_baseline import (
    NOT_YET_TRACKED_FIELDS,
    build_personal_baseline_report,
    compute_field_baselines,
)

TODAY = date(2026, 7, 22)


def entry(days_ago: int, **fields) -> dict:
    return {"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), **fields}


class TestComputeFieldBaselines:
    def test_no_entries_yields_missing_for_every_field(self):
        baselines = compute_field_baselines([], TODAY)
        assert set(baselines.keys()) == {"sleep_hours", "steps", "movement_minutes"}
        for fb in baselines.values():
            assert fb.recent.data_quality == "missing"
            assert fb.baseline.data_quality == "missing"
            assert fb.last_updated is None

    def test_tracks_last_updated_as_most_recent_entry_date(self):
        entries = [entry(5, sleep_hours=7), entry(0, sleep_hours=8)]
        baselines = compute_field_baselines(entries, TODAY)
        assert baselines["sleep_hours"].last_updated == TODAY.isoformat()


class TestBuildPersonalBaselineReport:
    def test_reports_not_enough_data_when_baseline_missing(self):
        report = build_personal_baseline_report([], TODAY)
        sleep_item = next(i for i in report["items"] if i["field"] == "sleep_hours")
        assert sleep_item["available"] is False
        assert sleep_item["message"] == "VitalTwin lernt noch deine persönliche Baseline."

    def test_never_invents_a_value_when_baseline_window_has_no_data(self):
        # Only recent-window data exists — the baseline window (days 7-34
        # ago, deliberately non-overlapping with the 7-day recent window)
        # has zero entries, so no comparison may be invented.
        entries = [entry(0, steps=9000), entry(1, steps=8000)]
        report = build_personal_baseline_report(entries, TODAY)
        steps_item = next(i for i in report["items"] if i["field"] == "steps")
        assert steps_item["available"] is False
        assert steps_item["message"] == "VitalTwin lernt noch deine persönliche Baseline."

    def test_percent_message_for_steps_above_baseline(self):
        # 28-day baseline average: 20 entries at 8000 steps/day.
        baseline_entries = [entry(i, steps=8000) for i in range(8, 28)]
        # 7-day recent average: 8960 steps/day (~12% above baseline).
        recent_entries = [entry(i, steps=8960) for i in range(7)]
        entries = recent_entries + baseline_entries

        report = build_personal_baseline_report(entries, TODAY)
        steps_item = next(i for i in report["items"] if i["field"] == "steps")
        assert steps_item["available"] is True
        assert steps_item["message"] == "Deine Schrittzahl liegt diese Woche 12% über deiner persönlichen 28-Tage-Baseline."

    def test_duration_message_for_sleep_below_baseline(self):
        baseline_entries = [entry(i, sleep_hours=7.5) for i in range(8, 28)]
        recent_entries = [entry(i, sleep_hours=7.1167) for i in range(7)]  # ~23 min shorter
        entries = recent_entries + baseline_entries

        report = build_personal_baseline_report(entries, TODAY)
        sleep_item = next(i for i in report["items"] if i["field"] == "sleep_hours")
        assert sleep_item["available"] is True
        assert "23 Minuten kürzer" in sleep_item["message"]

    def test_lists_not_yet_tracked_fields_honestly_instead_of_omitting_them(self):
        report = build_personal_baseline_report([], TODAY)
        tracked_fields = {item["field"] for item in report["not_yet_tracked"]}
        assert tracked_fields == set(NOT_YET_TRACKED_FIELDS)
        for item in report["not_yet_tracked"]:
            assert item["available"] is False

    def test_disclaimer_always_present(self):
        report = build_personal_baseline_report([], TODAY)
        assert "eigenen Verlauf" in report["disclaimer"]
