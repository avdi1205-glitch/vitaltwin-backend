"""Unit tests for `app.services.personalization` (Etappe 4 §6).

Pure functions — no database access.
"""

from datetime import date, timedelta

from app.services.personalization import (
    compute_category_penalty,
    has_recent_unsuccessful_duplicate,
    matches_preferred_time,
    should_deprioritize_category,
)

TODAY = date(2026, 7, 22)


class TestCategoryPenalty:
    def test_rejections_increase_penalty(self):
        history = [{"category": "schlaf", "status": "rejected"}, {"category": "schlaf", "status": "rejected"}]
        penalties = compute_category_penalty(history)
        assert penalties["schlaf"] == 2

    def test_acceptances_decrease_penalty(self):
        history = [
            {"category": "schlaf", "status": "rejected"},
            {"category": "schlaf", "status": "rejected"},
            {"category": "schlaf", "status": "accepted"},
        ]
        penalties = compute_category_penalty(history)
        assert penalties["schlaf"] == 1

    def test_ignores_rows_without_category(self):
        assert compute_category_penalty([{"status": "rejected"}]) == {}


class TestShouldDeprioritize:
    def test_below_threshold_is_not_deprioritized(self):
        assert should_deprioritize_category("schlaf", {"schlaf": 1}) is False

    def test_at_threshold_is_deprioritized(self):
        assert should_deprioritize_category("schlaf", {"schlaf": 2}) is True

    def test_unknown_category_defaults_to_not_deprioritized(self):
        assert should_deprioritize_category("unbekannt", {}) is False


class TestHasRecentUnsuccessfulDuplicate:
    def test_no_match_for_different_category(self):
        history = [{"category": "bewegung", "proposed_action": "x", "status": "rejected", "created_at": TODAY.isoformat()}]
        assert has_recent_unsuccessful_duplicate("schlaf", "x", history, today=TODAY) is False

    def test_matches_rejected_recent_recommendation(self):
        history = [{"category": "schlaf", "proposed_action": "x", "status": "rejected", "created_at": TODAY.isoformat()}]
        assert has_recent_unsuccessful_duplicate("schlaf", "x", history, today=TODAY) is True

    def test_matches_not_implemented_outcome(self):
        history = [
            {
                "category": "schlaf",
                "proposed_action": "x",
                "status": "completed",
                "outcome_status": "not_implemented",
                "created_at": TODAY.isoformat(),
            }
        ]
        assert has_recent_unsuccessful_duplicate("schlaf", "x", history, today=TODAY) is True

    def test_matches_not_helpful_feedback(self):
        history = [
            {
                "category": "schlaf",
                "proposed_action": "x",
                "status": "completed",
                "helpfulness": "not_helpful",
                "created_at": TODAY.isoformat(),
            }
        ]
        assert has_recent_unsuccessful_duplicate("schlaf", "x", history, today=TODAY) is True

    def test_old_recommendation_outside_cooldown_is_ignored(self):
        old_date = (TODAY - timedelta(days=30)).isoformat()
        history = [{"category": "schlaf", "proposed_action": "x", "status": "rejected", "created_at": old_date}]
        assert has_recent_unsuccessful_duplicate("schlaf", "x", history, today=TODAY) is False

    def test_successful_recommendation_does_not_block_repeat(self):
        history = [{"category": "schlaf", "proposed_action": "x", "status": "accepted", "created_at": TODAY.isoformat()}]
        assert has_recent_unsuccessful_duplicate("schlaf", "x", history, today=TODAY) is False


class TestMatchesPreferredTime:
    def test_no_reminder_time_is_false(self):
        assert matches_preferred_time(None, current_hour=8) is False

    def test_within_three_hours_matches(self):
        assert matches_preferred_time("07:00", current_hour=9) is True

    def test_outside_three_hours_does_not_match(self):
        assert matches_preferred_time("07:00", current_hour=20) is False

    def test_malformed_reminder_time_is_false(self):
        assert matches_preferred_time("not-a-time", current_hour=8) is False
