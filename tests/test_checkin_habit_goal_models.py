"""Unit tests for the Etappe 3 Pydantic validation models in
`app.routers.profile` (check-in, habit, goal). Pure model construction —
no network/database access (Supabase client creation doesn't connect at
import time)."""

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.routers.profile import (
    DailyWellnessEntryInput,
    GoalCreate,
    GoalUpdate,
    HabitCreate,
    HabitEntryInput,
    HabitUpdate,
)

TOMORROW = date.today() + timedelta(days=1)
YESTERDAY = date.today() - timedelta(days=1)


class TestDailyWellnessEntryInput:
    @pytest.mark.parametrize("field", ["mood", "motivation", "sleep_quality", "recovery", "energy", "stress"])
    def test_rejects_out_of_range_scale(self, field):
        with pytest.raises(ValidationError):
            DailyWellnessEntryInput(**{field: 11})
        with pytest.raises(ValidationError):
            DailyWellnessEntryInput(**{field: 0})

    @pytest.mark.parametrize("field", ["mood", "motivation", "sleep_quality", "recovery", "energy", "stress"])
    def test_accepts_valid_scale(self, field):
        model = DailyWellnessEntryInput(**{field: 7})
        assert getattr(model, field) == 7

    def test_rejects_negative_movement_minutes(self):
        with pytest.raises(ValidationError):
            DailyWellnessEntryInput(movement_minutes=-5)

    def test_rejects_future_entry_date(self):
        with pytest.raises(ValidationError):
            DailyWellnessEntryInput(entry_date=TOMORROW)

    def test_accepts_today_and_past_entry_date(self):
        assert DailyWellnessEntryInput(entry_date=date.today()).entry_date == date.today()
        assert DailyWellnessEntryInput(entry_date=YESTERDAY).entry_date == YESTERDAY

    def test_note_too_long_is_rejected(self):
        with pytest.raises(ValidationError):
            DailyWellnessEntryInput(note="x" * 281)

    def test_note_is_stripped(self):
        model = DailyWellnessEntryInput(note="  gut geschlafen  ")
        assert model.note == "gut geschlafen"


class TestHabitEntryInput:
    def test_rejects_future_entry_date(self):
        with pytest.raises(ValidationError):
            HabitEntryInput(entry_date=TOMORROW)


class TestHabitCreateStatus:
    def test_default_status_is_active(self):
        habit = HabitCreate(name="Spazieren", category="bewegung", frequency="taeglich")
        assert habit.status == "active"

    def test_rejects_invalid_status(self):
        with pytest.raises(ValidationError):
            HabitCreate(name="Spazieren", category="bewegung", frequency="taeglich", status="not-a-status")

    @pytest.mark.parametrize("status", ["active", "paused", "archived"])
    def test_accepts_all_valid_statuses(self, status):
        habit = HabitCreate(name="Spazieren", category="bewegung", frequency="taeglich", status=status)
        assert habit.status == status


class TestHabitUpdateStatus:
    def test_rejects_invalid_status(self):
        with pytest.raises(ValidationError):
            HabitUpdate(status="deleted")

    def test_none_status_is_allowed_no_op(self):
        assert HabitUpdate().status is None


class TestGoalCreate:
    def test_rejects_empty_title(self):
        with pytest.raises(ValidationError):
            GoalCreate(title="   ", goal_type="besser_schlafen")

    def test_rejects_unknown_goal_type(self):
        with pytest.raises(ValidationError):
            GoalCreate(title="Mein Ziel", goal_type="nicht_erlaubt")

    def test_accepts_custom_goal_type(self):
        goal = GoalCreate(title="Mein Ziel", goal_type="eigenes_ziel")
        assert goal.goal_type == "eigenes_ziel"

    def test_rejects_past_target_date(self):
        with pytest.raises(ValidationError):
            GoalCreate(title="Mein Ziel", goal_type="mehr_energie", target_date=YESTERDAY)

    def test_accepts_future_target_date(self):
        future = date.today() + timedelta(days=30)
        goal = GoalCreate(title="Mein Ziel", goal_type="mehr_energie", target_date=future)
        assert goal.target_date == future

    def test_rejects_invalid_status(self):
        with pytest.raises(ValidationError):
            GoalCreate(title="Mein Ziel", goal_type="mehr_energie", status="not-a-status")


class TestGoalUpdate:
    def test_rejects_invalid_status(self):
        with pytest.raises(ValidationError):
            GoalUpdate(status="not-a-status")

    @pytest.mark.parametrize("status", ["active", "paused", "completed", "archived"])
    def test_accepts_all_valid_statuses(self, status):
        assert GoalUpdate(status=status).status == status
