"""Unit tests for `app.services.recommendation_rules` (Etappe 4 §2).

Pure functions — no database access.
"""

from datetime import date, timedelta

from app.services.recommendation_rules import (
    evaluate_goal_rule,
    evaluate_habit_rule,
    evaluate_movement_rule,
    evaluate_sleep_rule,
    evaluate_stress_rule,
    generate_recommendations,
)

TODAY = date(2026, 7, 22)


def entry(days_ago: int, **fields) -> dict:
    return {"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), **fields}


class TestSleepRule:
    def test_no_recommendation_without_enough_data(self):
        entries = [entry(0, sleep_hours=5.0)]
        assert evaluate_sleep_rule(entries, today=TODAY) is None

    def test_no_recommendation_when_sleep_is_fine(self):
        entries = [entry(i, sleep_hours=8.0) for i in range(5)]
        assert evaluate_sleep_rule(entries, today=TODAY) is None

    def test_recommendation_on_repeated_short_sleep(self):
        entries = [entry(i, sleep_hours=5.0) for i in range(5)]
        draft = evaluate_sleep_rule(entries, today=TODAY)
        assert draft is not None
        assert draft.category == "schlaf"
        assert draft.rule_name == "repeated_short_sleep"
        assert draft.data_points == 5

    def test_two_short_nights_are_not_enough(self):
        entries = [entry(0, sleep_hours=5.0), entry(1, sleep_hours=5.0), entry(2, sleep_hours=8.0)]
        assert evaluate_sleep_rule(entries, today=TODAY) is None

    def test_ignores_entries_outside_lookback_window(self):
        entries = [entry(i, sleep_hours=5.0) for i in range(100, 105)]
        assert evaluate_sleep_rule(entries, today=TODAY) is None


class TestMovementRule:
    def test_no_recommendation_without_enough_data(self):
        entries = [entry(0, movement_minutes=5)]
        assert evaluate_movement_rule(entries, today=TODAY) is None

    def test_no_recommendation_when_movement_is_sufficient(self):
        entries = [entry(i, movement_minutes=30) for i in range(4)]
        assert evaluate_movement_rule(entries, today=TODAY) is None

    def test_recommendation_on_low_average_movement(self):
        entries = [entry(i, movement_minutes=5) for i in range(4)]
        draft = evaluate_movement_rule(entries, today=TODAY)
        assert draft is not None
        assert draft.category == "bewegung"


class TestStressRule:
    def test_no_recommendation_without_enough_data(self):
        entries = [entry(0, stress=9)]
        assert evaluate_stress_rule(entries, today=TODAY) is None

    def test_no_recommendation_when_stress_is_low(self):
        entries = [entry(i, stress=3) for i in range(4)]
        assert evaluate_stress_rule(entries, today=TODAY) is None

    def test_recommendation_on_high_average_stress(self):
        entries = [entry(i, stress=8) for i in range(4)]
        draft = evaluate_stress_rule(entries, today=TODAY)
        assert draft is not None
        assert draft.category == "stress"
        assert draft.priority == "high"


class TestHabitRule:
    def test_no_recommendation_for_completed_habit(self):
        habits = [{"id": "1", "name": "Laufen", "category": "bewegung", "status": "active", "completed_today": True, "completion_rate_7d": 0.1}]
        assert evaluate_habit_rule(habits) == []

    def test_no_recommendation_for_high_completion_rate(self):
        habits = [{"id": "1", "name": "Laufen", "category": "bewegung", "status": "active", "completed_today": False, "completion_rate_7d": 0.8}]
        assert evaluate_habit_rule(habits) == []

    def test_no_recommendation_for_paused_habit(self):
        habits = [{"id": "1", "name": "Laufen", "category": "bewegung", "status": "paused", "completed_today": False, "completion_rate_7d": 0.1}]
        assert evaluate_habit_rule(habits) == []

    def test_recommendation_for_open_low_completion_habit(self):
        habits = [{"id": "1", "name": "Laufen", "category": "bewegung", "status": "active", "completed_today": False, "completion_rate_7d": 0.2}]
        drafts = evaluate_habit_rule(habits)
        assert len(drafts) == 1
        assert drafts[0].habit_id == "1"


class TestGoalRule:
    def test_no_recommendation_for_inactive_goal(self):
        goals = [{"id": "1", "title": "Mehr laufen", "goal_type": "mehr_bewegen", "status": "paused"}]
        assert evaluate_goal_rule(goals) == []

    def test_recommendation_for_active_goal(self):
        goals = [{"id": "1", "title": "Mehr laufen", "goal_type": "mehr_bewegen", "status": "active"}]
        drafts = evaluate_goal_rule(goals)
        assert len(drafts) == 1
        assert drafts[0].goal_id == "1"


class TestGenerateRecommendations:
    def test_combines_all_rules(self):
        entries = [entry(i, sleep_hours=5.0, movement_minutes=5, stress=8) for i in range(5)]
        habits = [{"id": "1", "name": "Laufen", "category": "bewegung", "status": "active", "completed_today": False, "completion_rate_7d": 0.1}]
        goals = [{"id": "1", "title": "Mehr laufen", "goal_type": "mehr_bewegen", "status": "active"}]
        drafts = generate_recommendations(daily_entries=entries, habits=habits, goals=goals, today=TODAY)
        categories = {d.category for d in drafts}
        # 3 check-in rules (schlaf/bewegung/stress) + 1 habit draft (category
        # "bewegung", from the habit's own category) + 1 goal draft (category
        # "mehr_bewegen", from the goal's goal_type) = 5 drafts total.
        assert categories == {"schlaf", "bewegung", "stress", "mehr_bewegen"}
        assert len(drafts) == 5

    def test_empty_context_yields_no_recommendations(self):
        assert generate_recommendations(daily_entries=[], habits=[], goals=[], today=TODAY) == []
