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

    def test_daily_signals_none_runs_no_cross_domain_detectors(self):
        # Backward compatibility: existing callers that don't pass
        # `daily_signals` at all must keep working unchanged.
        patterns = pattern_detection.generate_patterns(
            daily_entries=[], habit_entries=[], recommendation_history=[], today=TODAY, daily_signals=None
        )
        assert patterns == []

    def test_daily_signals_provided_runs_cross_domain_detectors(self):
        daily_signals = {
            (TODAY - timedelta(days=idx)): {"google_steps": steps, "glucose_mean": glucose}
            for idx, (steps, glucose) in enumerate(
                zip([3000, 4000, 5000, 6000, 7000, 8000], [90, 95, 100, 105, 110, 115])
            )
        }
        patterns = pattern_detection.generate_patterns(
            daily_entries=[], habit_entries=[], recommendation_history=[], today=TODAY, daily_signals=daily_signals
        )
        pattern_types = {p.pattern_type for p in patterns}
        assert "aktivitaet_glukose_gleicher_tag" in pattern_types


class TestCrossDomainPatterns:
    """Twin Core Phase 3 — a LIMITED, explicitly-documented allowlist, each
    reusing the SAME `_detect_correlation_pattern`/`_pearson` as the
    same-table detectors above (no new statistical method)."""

    def _daily_signals(self, **series: list[float]) -> dict[date, dict[str, float]]:
        length = len(next(iter(series.values())))
        return {
            (TODAY - timedelta(days=idx)): {key: values[idx] for key, values in series.items()}
            for idx in range(length)
        }

    def test_activity_glucose_same_day_pattern_detected(self):
        daily_signals = self._daily_signals(
            google_steps=[3000, 4000, 5000, 6000, 7000, 8000], glucose_mean=[90, 95, 100, 105, 110, 115]
        )
        pattern = pattern_detection.detect_activity_glucose_same_day_pattern(daily_signals, today=TODAY)
        assert pattern is not None
        assert pattern.pattern_type == "aktivitaet_glukose_gleicher_tag"
        assert pattern.evidence["alignment"] == "same_day"
        assert pattern.evidence["sources"] == ["google_health", "cgm"]
        assert "verursacht" not in pattern.summary.lower()

    def test_activity_glucose_insufficient_overlapping_days_yields_none(self):
        # Only 3 days have BOTH signals — below MIN_PATTERN_DATA_POINTS=5.
        daily_signals = self._daily_signals(google_steps=[3000, 4000, 5000], glucose_mean=[90, 95, 100])
        assert pattern_detection.detect_activity_glucose_same_day_pattern(daily_signals, today=TODAY) is None

    def test_activity_glucose_constant_glucose_yields_none(self):
        # Non-constant-data rejection is inherited from `_pearson` (zero
        # variance -> None) — no new code needed for this gate.
        daily_signals = self._daily_signals(
            google_steps=[3000, 4000, 5000, 6000, 7000, 8000], glucose_mean=[100, 100, 100, 100, 100, 100]
        )
        assert pattern_detection.detect_activity_glucose_same_day_pattern(daily_signals, today=TODAY) is None

    def test_sleep_next_day_energy_pattern_uses_next_day_alignment_wording(self):
        # sleep on day N pairs with energy on day N+1: build a daily_signals
        # dict where sleep_hours(day) correlates with energy(day+1).
        days = [TODAY - timedelta(days=idx) for idx in range(7, -1, -1)]
        sleep_values = [5, 6, 7, 8, 9, 5, 6, 7]
        energy_values_next_day = [3, 4, 5, 6, 7, 3, 4]  # energy on day[i+1] correlates with sleep on day[i]
        daily_signals: dict[date, dict[str, float]] = {}
        for idx, day in enumerate(days):
            daily_signals.setdefault(day, {})["sleep_hours"] = sleep_values[idx]
        for idx, energy in enumerate(energy_values_next_day):
            daily_signals.setdefault(days[idx] + timedelta(days=1), {})["energy"] = energy

        pattern = pattern_detection.detect_sleep_next_day_energy_pattern(daily_signals, today=TODAY)
        assert pattern is not None
        assert pattern.evidence["alignment"] == "next_day"
        assert "Folgetag" in pattern.summary or "darauffolgenden Tag" in pattern.summary

    def test_sleep_next_day_glucose_pattern_detected(self):
        days = [TODAY - timedelta(days=idx) for idx in range(7, -1, -1)]
        sleep_values = [5, 6, 7, 8, 9, 5, 6, 7]
        glucose_next_day = [130, 125, 120, 115, 110, 130, 125]
        daily_signals: dict[date, dict[str, float]] = {}
        for idx, day in enumerate(days):
            daily_signals.setdefault(day, {})["sleep_hours"] = sleep_values[idx]
        for idx, glucose in enumerate(glucose_next_day):
            daily_signals.setdefault(days[idx] + timedelta(days=1), {})["glucose_mean"] = glucose

        pattern = pattern_detection.detect_sleep_next_day_glucose_pattern(daily_signals, today=TODAY)
        assert pattern is not None
        assert pattern.evidence["alignment"] == "next_day"
        assert pattern.evidence["sources"] == ["checkin", "cgm"]

    def test_nutrition_carbs_glucose_same_day_pattern_detected(self):
        daily_signals = self._daily_signals(
            nutrition_carbs=[20, 40, 60, 80, 100, 120], glucose_mean=[90, 100, 110, 120, 130, 140]
        )
        pattern = pattern_detection.detect_nutrition_carbs_glucose_same_day_pattern(daily_signals, today=TODAY)
        assert pattern is not None
        assert pattern.evidence["alignment"] == "same_day"
        assert pattern.evidence["sources"] == ["nutrition", "cgm"]

    def test_sparse_nutrition_logging_insufficient_overlap_yields_none(self):
        daily_signals = self._daily_signals(nutrition_carbs=[40, 60], glucose_mean=[100, 110])
        assert pattern_detection.detect_nutrition_carbs_glucose_same_day_pattern(daily_signals, today=TODAY) is None

    def test_no_causality_language_in_any_cross_domain_summary(self):
        daily_signals = self._daily_signals(
            google_steps=[3000, 4000, 5000, 6000, 7000, 8000],
            glucose_mean=[90, 95, 100, 105, 110, 115],
            nutrition_carbs=[20, 40, 60, 80, 100, 120],
        )
        for detector in (
            pattern_detection.detect_activity_glucose_same_day_pattern,
            pattern_detection.detect_nutrition_carbs_glucose_same_day_pattern,
        ):
            pattern = detector(daily_signals, today=TODAY)
            assert pattern is not None
            assert "verursacht" not in pattern.summary.lower()
            assert "diagnos" not in pattern.summary.lower()
            assert "möglicherweise" in pattern.summary

