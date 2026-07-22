"""Unit tests for the Weekly Reflection Loop
(`app.services.weekly_reflection`). Pure functions — no network/database
access."""

from __future__ import annotations

from app.services import weekly_reflection


class TestInsufficientData:
    def test_too_few_checkins_yields_fixed_disclaimer(self):
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=[{"entry_date": "2026-07-20", "sleep_hours": 7}],
            previous_week_entries=[],
            habits=[],
            goals=[],
            recommendation_history=[],
            confirmed_patterns=[],
        )
        assert result.data_sufficient is False
        assert result.summary == weekly_reflection.INSUFFICIENT_DATA_SUMMARY
        assert result.positive_developments == []
        assert result.potential_areas == []


class TestSufficientData:
    def _entries(self, dates: list[str], sleep_hours: list[float]) -> list[dict]:
        return [{"entry_date": d, "sleep_hours": s} for d, s in zip(dates, sleep_hours)]

    def test_improved_metric_is_a_positive_development(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [8.0, 8.0, 8.0])
        previous_week = self._entries(["2026-07-13", "2026-07-14", "2026-07-15"], [6.0, 6.0, 6.0])
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=previous_week,
            habits=[],
            goals=[],
            recommendation_history=[],
            confirmed_patterns=[],
        )
        assert result.data_sufficient is True
        assert any("Schlafdauer" in note for note in result.positive_developments)

    def test_worsened_metric_is_a_potential_area(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [5.0, 5.0, 5.0])
        previous_week = self._entries(["2026-07-13", "2026-07-14", "2026-07-15"], [8.0, 8.0, 8.0])
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=previous_week,
            habits=[],
            goals=[],
            recommendation_history=[],
            confirmed_patterns=[],
        )
        assert any("Schlafdauer" in note for note in result.potential_areas)

    def test_small_change_below_threshold_is_not_reported(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [7.0, 7.0, 7.0])
        previous_week = self._entries(["2026-07-13", "2026-07-14", "2026-07-15"], [7.1, 7.1, 7.1])
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=previous_week,
            habits=[],
            goals=[],
            recommendation_history=[],
            confirmed_patterns=[],
        )
        assert result.positive_developments == []
        assert result.potential_areas == []

    def test_stable_routine_is_detected(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [7.0, 7.0, 7.0])
        habits = [{"name": "Laufen", "status": "active", "completion_rate_7d": 0.9}]
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=[],
            habits=habits,
            goals=[],
            recommendation_history=[],
            confirmed_patterns=[],
        )
        assert any("Laufen" in note for note in result.stable_routines)

    def test_struggling_habit_is_a_potential_area(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [7.0, 7.0, 7.0])
        habits = [{"name": "Meditation", "status": "active", "completion_rate_7d": 0.1}]
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=[],
            habits=habits,
            goals=[],
            recommendation_history=[],
            confirmed_patterns=[],
        )
        assert any("Meditation" in note for note in result.potential_areas)

    def test_helpful_and_unhelpful_recommendations_are_grouped(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [7.0, 7.0, 7.0])
        recommendation_history = [
            {"category": "schlaf", "helpfulness": "helpful"},
            {"category": "schlaf", "helpfulness": "helpful"},
            {"category": "stress", "helpfulness": "not_helpful"},
        ]
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=[],
            habits=[],
            goals=[],
            recommendation_history=recommendation_history,
            confirmed_patterns=[],
        )
        assert any("schlaf" in note for note in result.most_helpful_recommendations)
        assert any("stress" in note for note in result.least_helpful_recommendations)

    def test_patterns_are_passed_through_unmodified(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [7.0, 7.0, 7.0])
        confirmed_patterns = [
            {"status": "active", "contradicting": False, "summary": "In deinen bisherigen Daten zeigt sich möglicherweise..."}
        ]
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=[],
            habits=[],
            goals=[],
            recommendation_history=[],
            confirmed_patterns=confirmed_patterns,
        )
        assert result.patterns == ["In deinen bisherigen Daten zeigt sich möglicherweise..."]

    def test_contradicting_pattern_is_excluded(self):
        this_week = self._entries(["2026-07-20", "2026-07-21", "2026-07-22"], [7.0, 7.0, 7.0])
        confirmed_patterns = [{"status": "active", "contradicting": True, "summary": "..."}]
        result = weekly_reflection.compute_weekly_reflection(
            this_week_entries=this_week,
            previous_week_entries=[],
            habits=[],
            goals=[],
            recommendation_history=[],
            confirmed_patterns=confirmed_patterns,
        )
        assert result.patterns == []
