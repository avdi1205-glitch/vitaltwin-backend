"""Unit tests for the Twin Context Engine (`app.services.twin_context`).
Pure functions — no network/database access."""

from __future__ import annotations

from app.services.twin_context import build_twin_context

EMPTY_KWARGS = dict(
    profile=None,
    goals=[],
    habits=[],
    daily_entry_count=0,
    trends={},
    confirmed_memories=[],
    active_recommendations=[],
    feedback_summary={},
    confirmed_patterns=[],
    daily_plan_actions=[],
    max_chars=2000,
)


class TestNoData:
    def test_empty_context_still_has_data_quality_note(self):
        context = build_twin_context(**EMPTY_KWARGS)
        assert "Noch keine Check-in-Daten" in context.text
        assert context.sources == []
        assert context.truncated is False


class TestProfileBlock:
    def test_profile_goals_are_included(self):
        kwargs = {**EMPTY_KWARGS, "profile": {"wellness_goals": ["besser schlafen"]}}
        context = build_twin_context(**kwargs)
        assert "besser schlafen" in context.text
        assert any(s.type == "user_reported" for s in context.sources)

    def test_missing_profile_is_skipped_without_error(self):
        context = build_twin_context(**EMPTY_KWARGS)
        assert context.text  # never raises, never empty (quality note always present)


class TestGoalsAndHabitsBlocks:
    def test_active_goal_is_included(self):
        kwargs = {**EMPTY_KWARGS, "goals": [{"title": "Mehr Energie", "status": "active"}]}
        context = build_twin_context(**kwargs)
        assert "Mehr Energie" in context.text

    def test_inactive_goal_is_excluded(self):
        kwargs = {**EMPTY_KWARGS, "goals": [{"title": "Pausiert", "status": "paused"}]}
        context = build_twin_context(**kwargs)
        assert "Pausiert" not in context.text

    def test_active_habit_with_completion_rate_is_included(self):
        kwargs = {**EMPTY_KWARGS, "habits": [{"name": "Laufen", "status": "active", "completion_rate_7d": 0.5}]}
        context = build_twin_context(**kwargs)
        assert "Laufen" in context.text
        assert "50%" in context.text


class TestTrendsBlock:
    def test_trend_with_average_is_included(self):
        kwargs = {**EMPTY_KWARGS, "trends": {"sleep_hours": {"average": 7.2, "data_quality": "calculated"}}}
        context = build_twin_context(**kwargs)
        assert "Schlafdauer" in context.text
        assert any(s.type == "trend" for s in context.sources)

    def test_trend_without_average_is_excluded(self):
        kwargs = {**EMPTY_KWARGS, "trends": {"sleep_hours": {"average": None, "data_quality": "missing"}}}
        context = build_twin_context(**kwargs)
        assert "Schlafdauer" not in context.text


class TestMemoriesBlock:
    def test_confirmed_memory_is_included(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_memories": [
                {"status": "confirmed", "human_readable_value": "Du meditierst meist um 7 Uhr."}
            ],
        }
        context = build_twin_context(**kwargs)
        assert "meditierst" in context.text
        assert any(s.type == "confirmed_memory" for s in context.sources)

    def test_active_memory_is_included(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_memories": [{"status": "active", "human_readable_value": "Aktive Beobachtung."}],
        }
        context = build_twin_context(**kwargs)
        assert "Aktive Beobachtung" in context.text

    def test_deleted_memory_is_never_included(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_memories": [
                {"status": "deleted", "human_readable_value": "Sollte niemals im Kontext auftauchen."}
            ],
        }
        context = build_twin_context(**kwargs)
        assert "Sollte niemals im Kontext auftauchen" not in context.text

    def test_candidate_memory_is_not_included(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_memories": [
                {"status": "candidate", "human_readable_value": "Nur eine Vermutung, noch nicht bestätigt."}
            ],
        }
        context = build_twin_context(**kwargs)
        assert "Nur eine Vermutung" not in context.text

    def test_disputed_memory_is_not_included(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_memories": [{"status": "disputed", "human_readable_value": "Wurde abgelehnt."}],
        }
        context = build_twin_context(**kwargs)
        assert "Wurde abgelehnt" not in context.text

    def test_archived_memory_is_not_included(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_memories": [{"status": "archived", "human_readable_value": "Archiviert."}],
        }
        context = build_twin_context(**kwargs)
        assert "Archiviert" not in context.text


class TestPatternsBlock:
    def test_active_non_contradicting_pattern_is_included(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_patterns": [
                {"status": "active", "contradicting": False, "summary": "In deinen Daten zeigt sich möglicherweise..."}
            ],
        }
        context = build_twin_context(**kwargs)
        assert "möglicherweise" in context.text
        assert any(s.type == "pattern" for s in context.sources)

    def test_contradicting_pattern_is_excluded(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_patterns": [{"status": "active", "contradicting": True, "summary": "Widersprüchlich."}],
        }
        context = build_twin_context(**kwargs)
        assert "Widersprüchlich" not in context.text

    def test_discarded_pattern_is_excluded(self):
        kwargs = {
            **EMPTY_KWARGS,
            "confirmed_patterns": [{"status": "discarded", "contradicting": False, "summary": "Verworfen."}],
        }
        context = build_twin_context(**kwargs)
        assert "Verworfen" not in context.text


class TestRecommendationsAndFeedbackBlocks:
    def test_proposed_recommendation_is_included(self):
        kwargs = {**EMPTY_KWARGS, "active_recommendations": [{"status": "proposed", "title": "Kleine Abendroutine"}]}
        context = build_twin_context(**kwargs)
        assert "Kleine Abendroutine" in context.text

    def test_accepted_recommendation_is_excluded_from_open_list(self):
        kwargs = {**EMPTY_KWARGS, "active_recommendations": [{"status": "accepted", "title": "Schon angenommen"}]}
        context = build_twin_context(**kwargs)
        assert "Schon angenommen" not in context.text

    def test_deprioritized_category_appears_in_feedback_summary(self):
        kwargs = {**EMPTY_KWARGS, "feedback_summary": {"bewegung": 2}}
        context = build_twin_context(**kwargs)
        assert "bewegung" in context.text

    def test_low_penalty_category_is_not_mentioned(self):
        kwargs = {**EMPTY_KWARGS, "feedback_summary": {"bewegung": 1}}
        context = build_twin_context(**kwargs)
        assert "bewegung" not in context.text


class TestDailyPlanBlock:
    def test_plan_actions_are_included(self):
        kwargs = {**EMPTY_KWARGS, "daily_plan_actions": [{"description": "10 Minuten spazieren gehen"}]}
        context = build_twin_context(**kwargs)
        assert "spazieren" in context.text

    def test_user_adjusted_description_takes_priority(self):
        kwargs = {
            **EMPTY_KWARGS,
            "daily_plan_actions": [{"description": "Original", "user_adjusted_description": "Angepasste Version"}],
        }
        context = build_twin_context(**kwargs)
        assert "Angepasste Version" in context.text
        assert "Original" not in context.text


class TestSizeCapping:
    def test_small_budget_truncates_lower_priority_blocks(self):
        kwargs = {
            **EMPTY_KWARGS,
            "goals": [{"title": "Mehr Energie", "status": "active"}],
            "confirmed_patterns": [
                {"status": "active", "contradicting": False, "summary": "Ein sehr langes Pattern " * 20}
            ],
            "max_chars": 80,
        }
        context = build_twin_context(**kwargs)
        assert context.truncated is True
        assert "Mehr Energie" in context.text  # higher priority block survives
        assert "Ein sehr langes Pattern" not in context.text  # lower priority block dropped

    def test_large_budget_is_not_truncated(self):
        kwargs = {**EMPTY_KWARGS, "goals": [{"title": "Ziel", "status": "active"}], "max_chars": 5000}
        context = build_twin_context(**kwargs)
        assert context.truncated is False


class TestForeignDataCannotLeak:
    def test_function_only_reflects_the_data_it_was_given(self):
        """Architectural guarantee: `build_twin_context` has no database
        access and no user identifier parameter — it can only ever surface
        exactly the rows the caller already fetched (and therefore already
        scoped to `email` in `routers/chat.py`). Passing a completely empty
        set of inputs must never produce any data out of nowhere."""
        context = build_twin_context(**EMPTY_KWARGS)
        assert context.sources == []
