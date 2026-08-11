"""Unit tests for `app.services.cgm_nutrition_signals` (Twin Core Phase 2
— CGM + Nutrition -> Twin Intelligence). Pure aggregation over
already-fetched rows, no database access — mirrors the testing style of
test_google_health_signals.py."""

from __future__ import annotations

from datetime import date, timedelta

from app.services import cgm_nutrition_signals as cns

TODAY = date(2026, 8, 11)


def cgm_row(days_ago: int, glucose: float, *, hour: int = 8) -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + f"T{hour:02d}:00:00+00:00"
    return {"email": "user-a@example.com", "glucose_value": glucose, "reading_at": ts, "source": "libreview"}


def nutrition_row(days_ago: int, *, calories: float = 0, protein: float = 0, carbs: float = 0, fat: float = 0, hour: int = 12) -> dict:
    ts = (TODAY - timedelta(days=days_ago)).isoformat() + f"T{hour:02d}:00:00+00:00"
    return {
        "email": "user-a@example.com",
        "meal_name": "Test",
        "calories": calories,
        "protein": protein,
        "carbs": carbs,
        "fat": fat,
        "logged_at": ts,
    }


class TestBuildCGMSignalNoData:
    def test_empty_rows_yield_no_data(self):
        signal = cns.build_cgm_signal([], today=TODAY)
        assert signal.has_data is False
        assert signal.reading_count == 0
        assert signal.coverage_days == 0
        assert signal.trend.average is None
        assert signal.trend.data_quality == "missing"


class TestBuildCGMSignalRealData:
    def test_many_daily_readings_are_averaged_per_day_not_overweighted(self):
        # 4 readings on day 0 (a real CGM emits many/day), 1 reading on day 1.
        rows = [
            cgm_row(0, 100, hour=6),
            cgm_row(0, 120, hour=12),
            cgm_row(0, 140, hour=18),
            cgm_row(0, 160, hour=23),
            cgm_row(1, 100),
        ]
        signal = cns.build_cgm_signal(rows, today=TODAY)
        assert signal.reading_count == 5
        assert signal.coverage_days == 2
        # day 0 mean = 130, day 1 mean = 100 -> trend average of the 2 days = 115
        assert signal.trend.average == 115.0

    def test_coverage_ratio_reflects_distinct_recorded_days(self):
        rows = [cgm_row(i, 100) for i in range(3)]  # 3 distinct days out of a 7-day window
        signal = cns.build_cgm_signal(rows, today=TODAY, window_days=7)
        assert signal.coverage_days == 3
        assert signal.coverage_ratio == round(3 / 7, 2)

    def test_latest_value_is_most_recent_reading(self):
        rows = [cgm_row(3, 90), cgm_row(0, 150)]
        signal = cns.build_cgm_signal(rows, today=TODAY)
        assert signal.latest_value == 150.0


class TestBuildCGMBaseline:
    def test_insufficient_baseline_history_returns_not_available(self):
        rows = [cgm_row(i, 100) for i in range(7)]
        baseline = cns.build_cgm_baseline(rows, today=TODAY)
        assert baseline.available is False
        assert "Noch nicht genügend" in baseline.message

    def test_valid_baseline_uses_non_overlapping_windows(self):
        recent = [cgm_row(i, 130) for i in range(7)]
        baseline_window = [cgm_row(i, 100) for i in range(8, 28)]
        rows = recent + baseline_window
        baseline = cns.build_cgm_baseline(rows, today=TODAY)
        assert baseline.available is True
        assert baseline.recent_average == 130.0
        assert baseline.baseline_average == 100.0


class TestBuildNutritionSignalNoData:
    def test_empty_rows_yield_no_data(self):
        signal = cns.build_nutrition_signal([], signal="energy_intake", today=TODAY)
        assert signal.has_data is False
        assert signal.entry_count == 0
        assert signal.logged_days == 0
        assert signal.trend.average is None


class TestBuildNutritionSignalRealData:
    def test_multiple_entries_per_day_are_summed_not_averaged(self):
        rows = [
            nutrition_row(0, calories=500, hour=8),
            nutrition_row(0, calories=700, hour=13),
            nutrition_row(0, calories=600, hour=19),
        ]
        signal = cns.build_nutrition_signal(rows, signal="energy_intake", today=TODAY)
        assert signal.trend.average == 1800.0  # summed daily total, not averaged per meal
        assert signal.unit == "kcal"

    def test_missing_day_is_not_treated_as_zero_intake(self):
        # Only day 0 and day 2 logged — day 1 has NO entries at all.
        rows = [nutrition_row(0, calories=2000), nutrition_row(2, calories=1800)]
        signal = cns.build_nutrition_signal(rows, signal="energy_intake", today=TODAY, window_days=3)
        # average over the 2 REAL logged days, never diluted by a fabricated 0-calorie day 1
        assert signal.trend.average == 1900.0
        assert signal.trend.data_points == 2
        assert signal.logged_days == 2

    def test_protein_carbs_fat_are_independent_signals(self):
        rows = [nutrition_row(0, protein=30, carbs=50, fat=20)]
        protein_signal = cns.build_nutrition_signal(rows, signal="protein", today=TODAY)
        carbs_signal = cns.build_nutrition_signal(rows, signal="carbohydrates", today=TODAY)
        fat_signal = cns.build_nutrition_signal(rows, signal="fat", today=TODAY)
        assert protein_signal.trend.average == 30.0
        assert carbs_signal.trend.average == 50.0
        assert fat_signal.trend.average == 20.0
        assert protein_signal.unit == "g"


class TestNutritionCoverageAndBaseline:
    def test_insufficient_logging_coverage_returns_not_available(self):
        # Only 2 of 7 recent days logged, and only 2 of 28 baseline days —
        # nowhere near the 50% minimum coverage required for a real baseline.
        rows = [nutrition_row(0, calories=2000), nutrition_row(1, calories=2000)] + [
            nutrition_row(10, calories=2000), nutrition_row(11, calories=2000)
        ]
        baseline = cns.build_nutrition_baseline(rows, signal="energy_intake", today=TODAY)
        assert baseline.available is False
        assert "unregelmäßig" in baseline.message

    def test_adequate_coverage_yields_valid_baseline(self):
        recent = [nutrition_row(i, calories=2000) for i in range(7)]  # 7/7 days logged
        baseline_window = [nutrition_row(i, calories=1800) for i in range(8, 28)]  # 20/28 days logged
        rows = recent + baseline_window
        baseline = cns.build_nutrition_baseline(rows, signal="energy_intake", today=TODAY)
        assert baseline.available is True
        assert baseline.recent_average == 2000.0
        assert baseline.baseline_average == 1800.0


class TestContextDictShapes:
    def test_cgm_signal_shape_carries_source_metadata(self):
        signal = cns.build_cgm_signal([cgm_row(0, 100)], today=TODAY)
        result = cns.cgm_to_context_dict(signal)
        assert result["has_data"] is True
        assert result["source"] == "cgm"
        assert result["unit"] == "mg/dL"
        assert result["reading_count"] == 1
        assert result["coverage_days"] == 1

    def test_no_data_cgm_has_none_source(self):
        signal = cns.build_cgm_signal([], today=TODAY)
        result = cns.cgm_to_context_dict(signal)
        assert result["has_data"] is False
        assert result["source"] == "none"

    def test_nutrition_signal_shape_carries_source_metadata(self):
        signal = cns.build_nutrition_signal([nutrition_row(0, calories=1900)], signal="energy_intake", today=TODAY)
        result = cns.nutrition_to_context_dict(signal)
        assert result["has_data"] is True
        assert result["source"] == "nutrition"
        assert result["unit"] == "kcal"
        assert result["entry_count"] == 1
