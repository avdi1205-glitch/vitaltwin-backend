"""Unit tests for Twin Memory detectors and lifecycle rules
(`app.services.twin_memory`). Pure functions — no network/database access."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services import twin_memory


TODAY = date(2026, 7, 22)
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


class TestDetectPreferredActivityTime:
    def test_high_completion_with_reminder_time_yields_candidate(self):
        habits = [{"id": "h1", "name": "Meditation", "reminder_time": "07:00", "completion_rate_30d": 0.8}]
        candidates = twin_memory.detect_preferred_activity_time(habits)
        assert len(candidates) == 1
        assert candidates[0].memory_type == "bevorzugte_aktivitaetszeit"
        assert candidates[0].memory_key == "preferred_time:h1"

    def test_low_completion_yields_nothing(self):
        habits = [{"id": "h1", "name": "Meditation", "reminder_time": "07:00", "completion_rate_30d": 0.3}]
        assert twin_memory.detect_preferred_activity_time(habits) == []

    def test_missing_reminder_time_yields_nothing(self):
        habits = [{"id": "h1", "name": "Meditation", "reminder_time": None, "completion_rate_30d": 0.9}]
        assert twin_memory.detect_preferred_activity_time(habits) == []


class TestDetectSuccessfulRoutine:
    def test_high_completion_and_long_streak_yields_candidate(self):
        habits = [{"id": "h1", "name": "Laufen", "completion_rate_30d": 0.9, "longest_streak": 14}]
        candidates = twin_memory.detect_successful_routine(habits)
        assert len(candidates) == 1
        assert candidates[0].memory_type == "erfolgreiche_routine"

    def test_short_streak_yields_nothing(self):
        habits = [{"id": "h1", "name": "Laufen", "completion_rate_30d": 0.95, "longest_streak": 2}]
        assert twin_memory.detect_successful_routine(habits) == []


class TestDetectRejectedRecommendationType:
    def test_repeated_rejection_yields_candidate(self):
        history = [
            {"category": "bewegung", "status": "rejected"},
            {"category": "bewegung", "status": "rejected"},
        ]
        candidates = twin_memory.detect_rejected_recommendation_type(history)
        assert len(candidates) == 1
        assert candidates[0].memory_type == "abgelehnter_empfehlungstyp"

    def test_single_rejection_yields_nothing(self):
        history = [{"category": "bewegung", "status": "rejected"}]
        assert twin_memory.detect_rejected_recommendation_type(history) == []


class TestDetectConfirmedPreference:
    def test_repeated_acceptance_yields_candidate(self):
        history = [
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "accepted"},
        ]
        candidates = twin_memory.detect_confirmed_preference(history)
        assert len(candidates) == 1
        assert candidates[0].memory_type == "bestaetigte_praeferenz"

    def test_mixed_acceptance_and_rejection_yields_nothing(self):
        history = [
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "accepted"},
            {"category": "schlaf", "status": "rejected"},
        ]
        assert twin_memory.detect_confirmed_preference(history) == []


class TestDetectActiveLongTermGoal:
    def test_goal_without_target_date_is_long_term(self):
        goals = [{"id": "g1", "title": "Mehr Energie", "status": "active", "target_date": None}]
        candidates = twin_memory.detect_active_long_term_goal(goals, today=TODAY)
        assert len(candidates) == 1

    def test_goal_with_near_target_date_is_not_long_term(self):
        goals = [
            {"id": "g1", "title": "Kurzfristig", "status": "active", "target_date": (TODAY.isoformat())}
        ]
        assert twin_memory.detect_active_long_term_goal(goals, today=TODAY) == []

    def test_inactive_goal_yields_nothing(self):
        goals = [{"id": "g1", "title": "Pausiert", "status": "paused", "target_date": None}]
        assert twin_memory.detect_active_long_term_goal(goals, today=TODAY) == []


class TestPromotePatternToMemory:
    def test_high_confidence_non_contradicting_pattern_promotes(self):
        pattern = {"id": "p1", "confidence": 0.8, "contradicting": False, "summary": "...", "pattern_type": "x"}
        candidate = twin_memory.promote_pattern_to_memory(pattern)
        assert candidate is not None
        assert candidate.memory_type == "bestaetigtes_muster"

    def test_low_confidence_pattern_does_not_promote(self):
        pattern = {"id": "p1", "confidence": 0.4, "contradicting": False}
        assert twin_memory.promote_pattern_to_memory(pattern) is None

    def test_contradicting_pattern_does_not_promote(self):
        pattern = {"id": "p1", "confidence": 0.9, "contradicting": True}
        assert twin_memory.promote_pattern_to_memory(pattern) is None


class TestGenerateMemoryCandidates:
    def test_combines_all_detectors(self):
        candidates = twin_memory.generate_memory_candidates(
            habits=[{"id": "h1", "name": "Laufen", "completion_rate_30d": 0.9, "longest_streak": 14}],
            goals=[{"id": "g1", "title": "Ziel", "status": "active", "target_date": None}],
            recommendation_history=[
                {"category": "schlaf", "status": "accepted"},
                {"category": "schlaf", "status": "accepted"},
            ],
            confirmed_patterns=[],
            today=TODAY,
        )
        types = {c.memory_type for c in candidates}
        assert types == {"erfolgreiche_routine", "aktives_langfristiges_ziel", "bestaetigte_praeferenz"}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestConfidenceHelpers:
    def test_bump_confidence_increases_and_clamps(self):
        assert twin_memory.bump_confidence(0.9, step=0.5) == twin_memory.MAX_CONFIDENCE

    def test_decay_confidence_decreases_and_clamps(self):
        assert twin_memory.decay_confidence(0.1, step=0.5) == twin_memory.MIN_CONFIDENCE

    def test_bump_confidence_defaults_when_none(self):
        assert twin_memory.bump_confidence(None) == pytest.approx(
            twin_memory.INITIAL_CANDIDATE_CONFIDENCE + twin_memory.CONFIRMATION_CONFIDENCE_STEP
        )


class TestPromoteAfterObservation:
    def test_promotes_candidate_after_enough_observations(self):
        assert twin_memory.promote_after_observation("candidate", observation_count=3) == "active"

    def test_stays_candidate_below_threshold(self):
        assert twin_memory.promote_after_observation("candidate", observation_count=2) == "candidate"

    def test_confirmed_status_is_never_downgraded_or_changed(self):
        assert twin_memory.promote_after_observation("confirmed", observation_count=10) == "confirmed"


class TestIsUsableForRecommendations:
    @pytest.mark.parametrize("status", ["active", "confirmed"])
    def test_usable_statuses(self, status):
        assert twin_memory.is_usable_for_recommendations(status) is True

    @pytest.mark.parametrize("status", ["candidate", "disputed", "archived", "deleted"])
    def test_non_usable_statuses(self, status):
        assert twin_memory.is_usable_for_recommendations(status) is False


class TestUserActions:
    def test_confirmation_sets_confirmed_status(self):
        memory = {"confidence": 0.5}
        updates = twin_memory.apply_user_confirmation(memory, now=NOW)
        assert updates["status"] == "confirmed"
        assert updates["user_confirmed"] is True
        assert updates["confidence"] > 0.5

    def test_correction_updates_value_and_confirms(self):
        memory = {"confidence": 0.4, "human_readable_value": "alt"}
        updates = twin_memory.apply_user_correction(
            memory, human_readable_value="neu", normalized_value={"x": 1}, now=NOW
        )
        assert updates["human_readable_value"] == "neu"
        assert updates["normalized_value"] == {"x": 1}
        assert updates["status"] == "confirmed"

    def test_rejection_marks_disputed_and_lowers_confidence(self):
        memory = {"confidence": 0.6}
        updates = twin_memory.apply_user_rejection(memory, now=NOW)
        assert updates["status"] == "disputed"
        assert updates["confidence"] < 0.6

    def test_archive_sets_archived_status(self):
        updates = twin_memory.apply_archive(NOW)
        assert updates["status"] == "archived"

    def test_deletion_sets_deleted_status_and_timestamp(self):
        updates = twin_memory.apply_deletion(NOW)
        assert updates["status"] == "deleted"
        assert updates["deleted_at"] == NOW.isoformat()


class TestIsExpired:
    def test_no_expiry_is_never_expired(self):
        assert twin_memory.is_expired(None, now=NOW) is False

    def test_past_expiry_is_expired(self):
        assert twin_memory.is_expired("2020-01-01T00:00:00+00:00", now=NOW) is True

    def test_future_expiry_is_not_expired(self):
        assert twin_memory.is_expired("2030-01-01T00:00:00+00:00", now=NOW) is False


class TestReevaluateDependentCandidates:
    def test_same_type_candidate_is_reset_and_confidence_lowered(self):
        others = [{"id": "m2", "memory_type": "bevorzugte_aktivitaetszeit", "status": "active", "confidence": 0.7}]
        updates = twin_memory.reevaluate_dependent_candidates("bevorzugte_aktivitaetszeit", others, now=NOW)
        assert len(updates) == 1
        memory_id, payload = updates[0]
        assert memory_id == "m2"
        assert payload["status"] == "candidate"
        assert payload["confidence"] < 0.7

    def test_different_type_is_untouched(self):
        others = [{"id": "m2", "memory_type": "erfolgreiche_routine", "status": "active", "confidence": 0.7}]
        assert twin_memory.reevaluate_dependent_candidates("bevorzugte_aktivitaetszeit", others, now=NOW) == []

    def test_confirmed_memory_is_left_alone(self):
        others = [{"id": "m2", "memory_type": "bevorzugte_aktivitaetszeit", "status": "confirmed", "confidence": 0.9}]
        assert twin_memory.reevaluate_dependent_candidates("bevorzugte_aktivitaetszeit", others, now=NOW) == []
