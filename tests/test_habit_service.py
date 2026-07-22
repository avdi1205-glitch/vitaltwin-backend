"""Unit tests for `app.services.habit_service.compute_habit_stats`
(Etappe 3). Wires `streaks.py` to raw `vt_habit_entries`-shaped rows.
"""

from datetime import date, timedelta

from app.services.habit_service import compute_habit_stats

TODAY = date(2026, 7, 22)


def iso_days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


class TestComputeHabitStats:
    def test_no_entries(self):
        stats = compute_habit_stats([], habit_created_at=None, today=TODAY)
        assert stats["completed_today"] is False
        assert stats["current_streak"] == 0
        assert stats["longest_streak"] == 0
        assert stats["completion_rate_7d"] == 0.0
        assert stats["completion_rate_30d"] == 0.0

    def test_ignores_uncompleted_entries(self):
        entries = [{"entry_date": iso_days_ago(0), "completed": False}]
        stats = compute_habit_stats(entries, habit_created_at=None, today=TODAY)
        assert stats["current_streak"] == 0

    def test_streak_from_real_entries(self):
        entries = [
            {"entry_date": iso_days_ago(0), "completed": True},
            {"entry_date": iso_days_ago(1), "completed": True},
            {"entry_date": iso_days_ago(2), "completed": True},
        ]
        stats = compute_habit_stats(entries, habit_created_at=None, today=TODAY)
        assert stats["completed_today"] is True
        assert stats["current_streak"] == 3
        assert stats["longest_streak"] == 3

    def test_completion_rate_window_is_clamped_to_habit_age(self):
        # Habit created 3 days ago, completed every day since -> should be
        # 100% for a 7-day window, not penalized for days before it existed.
        created_at = f"{iso_days_ago(2)}T00:00:00+00:00"
        entries = [
            {"entry_date": iso_days_ago(0), "completed": True},
            {"entry_date": iso_days_ago(1), "completed": True},
            {"entry_date": iso_days_ago(2), "completed": True},
        ]
        stats = compute_habit_stats(entries, habit_created_at=created_at, today=TODAY)
        assert stats["completion_rate_7d"] == 1.0

    def test_malformed_created_at_falls_back_to_unclamped_window(self):
        entries = [{"entry_date": iso_days_ago(0), "completed": True}]
        stats = compute_habit_stats(entries, habit_created_at="not-a-date", today=TODAY)
        # Should not raise, and should still compute a valid (lower) rate.
        assert 0.0 <= stats["completion_rate_7d"] <= 1.0
