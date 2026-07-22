"""Unit tests for rule-based pattern detection
(`app.services.pattern_detection`). Pure functions — no network/database
access."""

from __future__ import annotations

from datetime import date, timedelta

from app.services import pattern_detection

TODAY = date(2026, 7, 22)


def _entries_from_pairs(field_a: str, field_b: str, a_values: list[float], b_values: list[float]) -> list[dict]:
    return [
        {"entry_date": (TODAY - timedelta(days=idx)).isoformat(), field_a: a, field_b: b}
        for idx, (a, b) in enumerate(zip(a_values, b_values))
    ]


class TestSleepEnergyPattern:
    def test_positive_correlation_is_detected(self):
        entries = _entries_from_pairs("sleep_hours", "energy", [5, 6, 7, 8, 9, 10], [3, 4, 5, 6, 7, 8])
        pattern = pattern_detection.detect_sleep_energy_pattern(entries, today=TODAY)
        assert pattern is not None
        assert pattern.direction == "positiv"
        assert pattern.contradicting is False
        assert "möglicherweise" in pattern.summary
        assert "verursacht" not in pattern.summary.lower()

    def test_too_few_data_points_yields_none(self):
        entries = _entries_from_pairs("sleep_hours", "energy", [5, 6, 7], [3, 4, 5])
        assert pattern_detection.detect_sleep_energy_pattern(entries, today=TODAY) is None

    def test_weak_correlation_yields_none(self):
        entries = _entries_from_pairs(
            "sleep_hours", "energy", [1, 2, 3, 4, 5, 6, 7], [4, 3, 5, 2, 6, 1, 7]
        )
        assert pattern_detection.detect_sleep_energy_pattern(entries, today=TODAY) is None

    def test_contradicting_data_is_flagged(self):
        # First half (oldest 3): strong positive correlation.
        # Second half (newest 3): strong negative correlation.
        entries = _entries_from_pairs(
            "sleep_hours", "energy", [1, 2, 3, 4, 5, 6], [1, 2, 3, 6, 5, 4]
        )
        pattern = pattern_detection.detect_sleep_energy_pattern(entries, today=TODAY)
        assert pattern is not None
        assert pattern.contradicting is True
        assert "nicht eindeutig" in pattern.summary


class TestMovementMoodPattern:
    def test_negative_correlation_is_detected(self):
        entries = _entries_from_pairs(
            "movement_minutes", "mood", [10, 20, 30, 40, 50, 60], [8, 7, 6, 5, 4, 3]
        )
        pattern = pattern_detection.detect_movement_mood_pattern(entries, today=TODAY)
        assert pattern is not None
        assert pattern.direction == "negativ"


class TestStressSleepQualityPattern:
    def test_negative_correlation_is_detected(self):
        entries = _entries_from_pairs("stress", "sleep_quality", [2, 3, 4, 5, 6, 7], [9, 8, 7, 6, 5, 4])
        pattern = pattern_detection.detect_stress_sleep_quality_pattern(entries, today=TODAY)
        assert pattern is not None
        assert pattern.direction == "negativ"


class TestWeekdayRoutinePattern:
    def _build_habit_entries(self) -> list[dict]:
        entries = []
        for i in range(21):
            d = TODAY - timedelta(days=i)
            weekday = d.weekday()
            if weekday == 0:  # Monday: always completed
                completed = True
            elif weekday == 4:  # Friday: never completed
                completed = False
            else:
                completed = i % 2 == 0
            entries.append({"entry_date": d.isoformat(), "completed": completed})
        return entries

    def test_clear_weekday_gap_is_detected(self):
        pattern = pattern_detection.detect_weekday_routine_pattern(self._build_habit_entries(), today=TODAY)
        assert pattern is not None
        assert pattern.pattern_type == "wochentag_routine"
        assert pattern.evidence["best_day"] == "Montag"
        assert pattern.evidence["worst_day"] == "Freitag"

    def test_insufficient_data_yields_none(self):
        entries = [{"entry_date": TODAY.isoformat(), "completed": True}]
        assert pattern_detection.detect_weekday_routine_pattern(entries, today=TODAY) is None


class TestRecommendationSuccessPattern:
    def test_clear_category_gap_is_detected(self):
        history = [
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "accepted"},
            {"category": "stress", "status": "rejected"},
            {"category": "stress", "status": "rejected"},
            {"category": "stress", "status": "rejected"},
        ]
        pattern = pattern_detection.detect_recommendation_success_pattern(history)
        assert pattern is not None
        assert pattern.evidence["best_category"] == "schlaf"
        assert pattern.evidence["worst_category"] == "stress"

    def test_insufficient_total_points_yields_none(self):
        history = [{"category": "schlaf", "status": "accepted"}]
        assert pattern_detection.detect_recommendation_success_pattern(history) is None

    def test_single_category_yields_none(self):
        history = [
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "rejected"},
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "rejected"},
        ]
        assert pattern_detection.detect_recommendation_success_pattern(history) is None


class TestGeneratePatterns:
    def test_combines_all_detectors(self):
        daily_entries = _entries_from_pairs("sleep_hours", "energy", [5, 6, 7, 8, 9, 10], [3, 4, 5, 6, 7, 8])
        habit_entries = TestWeekdayRoutinePattern()._build_habit_entries()
        recommendation_history = [
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "accepted"},
            {"category": "stress", "status": "rejected"},
            {"category": "stress", "status": "rejected"},
            {"category": "stress", "status": "rejected"},
        ]
        patterns = pattern_detection.generate_patterns(
            daily_entries=daily_entries,
            habit_entries=habit_entries,
            recommendation_history=recommendation_history,
            today=TODAY,
        )
        pattern_types = {p.pattern_type for p in patterns}
        assert "schlafdauer_energie" in pattern_types
        assert "wochentag_routine" in pattern_types
        assert "empfehlungstyp_erfolgsquote" in pattern_types
