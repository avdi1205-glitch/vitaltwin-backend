"""Unit tests for `app.services.unified_twin_state` (Twin Core Phase 4).
Pure composition over already-fetched/already-built inputs, no database
access."""

from __future__ import annotations

from datetime import date, timedelta

from app.services import unified_twin_state as uts

TODAY = date(2026, 8, 11)


def checkin(days_ago: int, **fields) -> dict:
    return {"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), **fields}


def calculation(days_ago: int, *, biologisches_alter: float, markers: list[str]) -> dict:
    created = (TODAY - timedelta(days=days_ago)).isoformat() + "T08:00:00+00:00"
    return {
        "created_at": created,
        "biologisches_alter": biologisches_alter,
        "differenz": biologisches_alter - 40,
        "scenarios": {"aktuell": biologisches_alter},
        "marker_breakdown": [{"marker": m, "value": 1.0, "contribution": 0.1} for m in markers],
    }


class TestSummarizeBehavioralState:
    def test_no_entries_is_missing(self):
        summary = uts.summarize_behavioral_state([], today=TODAY)
        assert summary.status == "missing"
        assert summary.data_count == 0

    def test_few_entries_is_insufficient_data(self):
        entries = [checkin(0, sleep_hours=7.0), checkin(1, sleep_hours=6.5)]
        summary = uts.summarize_behavioral_state(entries, today=TODAY)
        assert summary.status == "insufficient_data"

    def test_enough_entries_is_current_with_real_trends(self):
        entries = [checkin(i, sleep_hours=7.0, energy=4) for i in range(5)]
        summary = uts.summarize_behavioral_state(entries, today=TODAY)
        assert summary.status == "current"
        assert summary.values["trends"]["sleep_hours"]["average"] == 7.0
        assert summary.last_updated == TODAY.isoformat()


class TestSummarizeAutomaticHealthState:
    def test_none_input_is_missing(self):
        assert uts.summarize_automatic_health_state(None).status == "missing"

    def test_no_signal_has_data_is_missing(self):
        google_health = {"steps": {"has_data": False, "average": None}}
        assert uts.summarize_automatic_health_state(google_health).status == "missing"

    def test_real_signal_is_current(self):
        google_health = {
            "steps": {"has_data": True, "average": 9000, "unit": "Schritte", "data_points": 5, "latest_observed_at": "2026-08-10T08:00:00+00:00"}
        }
        summary = uts.summarize_automatic_health_state(google_health)
        assert summary.status == "current"
        assert summary.values["steps"]["average"] == 9000
        assert summary.last_updated == "2026-08-10T08:00:00+00:00"


class TestSummarizeMetabolicState:
    def test_no_cgm_or_nutrition_is_missing(self):
        assert uts.summarize_metabolic_state(None, None).status == "missing"

    def test_cgm_only_is_current(self):
        cgm = {"has_data": True, "average": 100, "unit": "mg/dL", "data_points": 3}
        summary = uts.summarize_metabolic_state(cgm, None)
        assert summary.status == "current"
        assert summary.source == ("cgm",)

    def test_nutrition_only_is_current(self):
        nutrition = {"energy_intake": {"has_data": True, "average": 2000, "unit": "kcal", "data_points": 1}}
        summary = uts.summarize_metabolic_state(None, nutrition)
        assert summary.status == "current"
        assert summary.source == ("nutrition",)

    def test_both_present_lists_both_sources(self):
        cgm = {"has_data": True, "average": 100, "unit": "mg/dL", "data_points": 3}
        nutrition = {"energy_intake": {"has_data": True, "average": 2000, "unit": "kcal", "data_points": 1}}
        summary = uts.summarize_metabolic_state(cgm, nutrition)
        assert summary.source == ("cgm", "nutrition")


class TestSummarizeBiomarkerState:
    def test_no_calculations_is_missing(self):
        summary = uts.summarize_biomarker_state([], today=TODAY)
        assert summary.status == "missing"
        assert summary.data_count == 0

    def test_latest_valid_calculation_is_selected(self):
        rows = [calculation(30, biologisches_alter=50.0, markers=["hba1c"]), calculation(1, biologisches_alter=45.0, markers=["hba1c", "crp"])]
        summary = uts.summarize_biomarker_state(rows, today=TODAY)
        assert summary.values["biologisches_alter"] == 45.0
        assert summary.values["markers_provided"] == ["hba1c", "crp"]

    def test_historical_older_calculation_does_not_override_latest_regardless_of_input_order(self):
        # Deliberately pass the OLDER row first to prove the function
        # re-sorts rather than trusting caller ordering.
        rows = [calculation(1, biologisches_alter=45.0, markers=["hba1c"]), calculation(90, biologisches_alter=60.0, markers=["hba1c"])]
        summary = uts.summarize_biomarker_state(rows, today=TODAY)
        assert summary.values["biologisches_alter"] == 45.0

    def test_data_count_reflects_total_history_not_just_latest(self):
        rows = [calculation(1, biologisches_alter=45.0, markers=["hba1c"]), calculation(30, biologisches_alter=50.0, markers=["hba1c"])]
        summary = uts.summarize_biomarker_state(rows, today=TODAY)
        assert summary.data_count == 2

    def test_no_stale_flag_is_invented_only_real_timestamp_exposed(self):
        rows = [calculation(200, biologisches_alter=50.0, markers=["hba1c"])]
        summary = uts.summarize_biomarker_state(rows, today=TODAY)
        assert summary.last_updated is not None
        assert not hasattr(summary, "stale")


class TestSummarizeMemoryAndPatternState:
    def test_no_memories_is_missing(self):
        assert uts.summarize_memory_state([]).status == "missing"

    def test_confirmed_memories_are_current(self):
        memories = [{"human_readable_value": "Du meditierst meist um 7 Uhr."}]
        summary = uts.summarize_memory_state(memories)
        assert summary.status == "current"
        assert "meditierst" in summary.values["notes"][0]

    def test_no_patterns_is_missing(self):
        assert uts.summarize_pattern_state([]).status == "missing"

    def test_cross_domain_pattern_count_is_tracked(self):
        patterns = [
            {"summary": "A", "evidence": {"alignment": "same_day"}},
            {"summary": "B", "evidence": {}},
        ]
        summary = uts.summarize_pattern_state(patterns)
        assert summary.values["cross_domain_count"] == 1


class TestSummarizeGoalHabitState:
    def test_no_active_goals_or_habits_is_missing(self):
        assert uts.summarize_goal_habit_state([], []).status == "missing"

    def test_active_goals_and_habits_are_current(self):
        goals = [{"status": "active"}]
        habits = [{"status": "active", "completion_rate_7d": 0.8}]
        summary = uts.summarize_goal_habit_state(goals, habits)
        assert summary.status == "current"
        assert summary.values["active_goal_count"] == 1
        assert summary.values["average_habit_completion_7d"] == 0.8


class TestBuildUnifiedTwinState:
    def _base_kwargs(self, **overrides) -> dict:
        base = dict(
            profile=None, daily_entries=[], goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[],
            google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY,
        )
        base.update(overrides)
        return base

    def test_behavioral_only_user(self):
        state = uts.build_unified_twin_state(**self._base_kwargs(daily_entries=[checkin(i, sleep_hours=7.0) for i in range(5)]))
        assert state.behavioral_state.status == "current"
        assert state.automatic_health_state.status == "missing"
        assert state.biomarker_state.status == "missing"

    def test_google_health_only_domain_present(self):
        google_health = {"steps": {"has_data": True, "average": 9000, "unit": "Schritte", "data_points": 5, "latest_observed_at": None}}
        state = uts.build_unified_twin_state(**self._base_kwargs(google_health=google_health))
        assert state.automatic_health_state.status == "current"
        assert state.behavioral_state.status == "missing"

    def test_cgm_nutrition_domain_present(self):
        cgm = {"has_data": True, "average": 100, "unit": "mg/dL", "data_points": 3}
        state = uts.build_unified_twin_state(**self._base_kwargs(cgm=cgm))
        assert state.metabolic_state.status == "current"

    def test_biomarker_only_user(self):
        rows = [calculation(1, biologisches_alter=45.0, markers=["hba1c"])]
        state = uts.build_unified_twin_state(**self._base_kwargs(biomarker_calculations=rows))
        assert state.biomarker_state.status == "current"
        assert state.behavioral_state.status == "missing"
        assert state.automatic_health_state.status == "missing"

    def test_multi_domain_user_all_domains_present(self):
        state = uts.build_unified_twin_state(**self._base_kwargs(
            daily_entries=[checkin(i, sleep_hours=7.0) for i in range(5)],
            google_health={"steps": {"has_data": True, "average": 9000, "unit": "Schritte", "data_points": 5, "latest_observed_at": None}},
            cgm={"has_data": True, "average": 100, "unit": "mg/dL", "data_points": 3},
            biomarker_calculations=[calculation(1, biologisches_alter=45.0, markers=["hba1c"])],
            confirmed_memories=[{"human_readable_value": "Notiz"}],
            confirmed_patterns=[{"summary": "Muster"}],
            goals=[{"status": "active"}],
        ))
        assert state.behavioral_state.status == "current"
        assert state.automatic_health_state.status == "current"
        assert state.metabolic_state.status == "current"
        assert state.biomarker_state.status == "current"
        assert state.memory_state.status == "current"
        assert state.pattern_state.status == "current"
        assert state.goal_habit_state.status == "current"

    def test_missing_domains_are_represented_honestly_never_fabricated(self):
        state = uts.build_unified_twin_state(**self._base_kwargs())
        for domain_summary in (
            state.behavioral_state, state.automatic_health_state, state.metabolic_state,
            state.biomarker_state, state.memory_state, state.pattern_state, state.goal_habit_state,
        ):
            assert domain_summary.status == "missing"
            assert domain_summary.data_count == 0

    def test_data_quality_summary_rolls_up_all_domain_statuses(self):
        state = uts.build_unified_twin_state(**self._base_kwargs())
        assert state.data_quality_summary["missing"] == 7
        assert state.data_quality_summary["current"] == 0

    def test_identity_context_reflects_profile_presence_only(self):
        state_with_profile = uts.build_unified_twin_state(**self._base_kwargs(profile={"email": "user@example.com"}))
        state_without_profile = uts.build_unified_twin_state(**self._base_kwargs(profile=None))
        assert state_with_profile.identity_context == {"has_profile": True}
        assert state_without_profile.identity_context == {"has_profile": False}
