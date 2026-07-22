"""Unit tests for the Daily Planning Loop
(`app.services.daily_planning`). Pure functions — no network/database
access."""

from __future__ import annotations

from app.services import daily_planning


class TestNoData:
    def test_empty_inputs_yield_no_actions(self):
        actions = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[], recommendations=[], yesterday_actions=[], current_hour=9
        )
        assert actions == []


class TestWithCheckinBasedRecommendation:
    def test_active_recommendation_yields_an_action(self):
        recommendations = [
            {
                "id": "r1",
                "status": "proposed",
                "priority": "high",
                "confidence": 0.8,
                "title": "Kleine Abendroutine",
                "proposed_action": "30 Minuten vor dem Schlafengehen Bildschirme weglegen.",
            }
        ]
        actions = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[], recommendations=recommendations, yesterday_actions=[], current_hour=9
        )
        assert len(actions) == 1
        assert actions[0].source == "recommendation"
        assert actions[0].recommendation_id == "r1"

    def test_non_proposed_recommendation_is_ignored(self):
        recommendations = [{"id": "r1", "status": "accepted", "priority": "high", "confidence": 0.8}]
        actions = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[], recommendations=recommendations, yesterday_actions=[], current_hour=9
        )
        assert actions == []


class TestWithGoals:
    def test_active_goal_yields_an_action(self):
        goals = [{"id": "g1", "title": "Mehr Energie", "status": "active"}]
        actions = daily_planning.generate_daily_plan_actions(
            goals=goals, habits=[], recommendations=[], yesterday_actions=[], current_hour=9
        )
        assert len(actions) == 1
        assert actions[0].source == "goal"
        assert actions[0].goal_id == "g1"

    def test_inactive_goal_is_ignored(self):
        goals = [{"id": "g1", "title": "Pausiert", "status": "paused"}]
        actions = daily_planning.generate_daily_plan_actions(
            goals=goals, habits=[], recommendations=[], yesterday_actions=[], current_hour=9
        )
        assert actions == []


class TestWithHabits:
    def test_open_active_habit_yields_an_action(self):
        habits = [
            {"id": "h1", "name": "Laufen", "status": "active", "completed_today": False, "completion_rate_7d": 0.2}
        ]
        actions = daily_planning.generate_daily_plan_actions(
            goals=[], habits=habits, recommendations=[], yesterday_actions=[], current_hour=9
        )
        assert len(actions) == 1
        assert actions[0].source == "habit"
        assert actions[0].habit_id == "h1"

    def test_completed_today_habit_is_ignored(self):
        habits = [
            {"id": "h1", "name": "Laufen", "status": "active", "completed_today": True, "completion_rate_7d": 0.2}
        ]
        actions = daily_planning.generate_daily_plan_actions(
            goals=[], habits=habits, recommendations=[], yesterday_actions=[], current_hour=9
        )
        assert actions == []

    def test_preferred_time_match_increases_score(self):
        habit_no_match = {
            "id": "h1",
            "name": "Meditation",
            "status": "active",
            "completed_today": False,
            "completion_rate_7d": 0.5,
            "reminder_time": "07:00",
        }
        actions_far = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[habit_no_match], recommendations=[], yesterday_actions=[], current_hour=20
        )
        actions_close = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[habit_no_match], recommendations=[], yesterday_actions=[], current_hour=7
        )
        assert actions_close[0].score > actions_far[0].score

    def test_confirmed_preferred_time_memory_increases_score(self):
        habit = {
            "id": "h1",
            "name": "Meditation",
            "status": "active",
            "completed_today": False,
            "completion_rate_7d": 0.5,
        }
        without_memory = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[habit], recommendations=[], yesterday_actions=[], current_hour=9
        )
        with_memory = daily_planning.generate_daily_plan_actions(
            goals=[],
            habits=[habit],
            recommendations=[],
            yesterday_actions=[],
            preferred_time_habit_ids={"h1"},
            current_hour=9,
        )
        assert with_memory[0].score > without_memory[0].score


class TestMaxThreeActions:
    def test_more_than_three_candidates_are_capped(self):
        goals = [{"id": f"g{i}", "title": f"Ziel {i}", "status": "active"} for i in range(5)]
        actions = daily_planning.generate_daily_plan_actions(
            goals=goals, habits=[], recommendations=[], yesterday_actions=[], current_hour=9
        )
        assert len(actions) == daily_planning.MAX_DAILY_PLAN_ACTIONS == 3


class TestCarriedOver:
    def test_open_yesterday_action_is_carried_over(self):
        yesterday_actions = [{"description": "Noch offen", "status": "accepted"}]
        actions = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[], recommendations=[], yesterday_actions=yesterday_actions, current_hour=9
        )
        assert len(actions) == 1
        assert actions[0].carried_over is True

    def test_completed_yesterday_action_is_not_carried_over(self):
        yesterday_actions = [{"description": "Erledigt", "status": "completed"}]
        actions = daily_planning.generate_daily_plan_actions(
            goals=[], habits=[], recommendations=[], yesterday_actions=yesterday_actions, current_hour=9
        )
        assert actions == []

    def test_carried_over_goal_is_not_duplicated_with_fresh_goal_candidate(self):
        yesterday_actions = [{"description": "Für Ziel", "status": "accepted", "goal_id": "g1"}]
        goals = [{"id": "g1", "title": "Mehr Energie", "status": "active"}]
        actions = daily_planning.generate_daily_plan_actions(
            goals=goals, habits=[], recommendations=[], yesterday_actions=yesterday_actions, current_hour=9
        )
        assert len(actions) == 1
        assert actions[0].carried_over is True


class TestNextStatusAfterAdjustment:
    def test_proposed_becomes_modified(self):
        assert daily_planning.next_status_after_adjustment("proposed") == "modified"

    def test_already_decided_status_is_unchanged(self):
        assert daily_planning.next_status_after_adjustment("accepted") == "accepted"
        assert daily_planning.next_status_after_adjustment("completed") == "completed"
