"""Unit tests for `app.services.streaks` (Habit Loop, Etappe 3).

Pure date-math, no database access.
"""

from datetime import date, timedelta

import pytest

from app.services.streaks import (
    compute_completion_rate,
    compute_current_streak,
    compute_longest_streak,
)

TODAY = date(2026, 7, 22)


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


class TestCurrentStreak:
    def test_empty_history_is_zero(self):
        assert compute_current_streak(set(), today=TODAY) == 0

    def test_completed_today_and_yesterday_counts_both(self):
        dates = {TODAY, days_ago(1)}
        assert compute_current_streak(dates, today=TODAY) == 2

    def test_gap_breaks_the_streak(self):
        dates = {TODAY, days_ago(1), days_ago(3)}  # gap at days_ago(2)
        assert compute_current_streak(dates, today=TODAY) == 2

    def test_not_yet_done_today_still_counts_streak_up_to_yesterday(self):
        # Mirrors the existing frontend behavior: an in-progress day isn't
        # shown as "streak broken" before the day is over.
        dates = {days_ago(1), days_ago(2), days_ago(3)}
        assert compute_current_streak(dates, today=TODAY) == 3

    def test_missed_yesterday_and_today_resets_to_zero(self):
        dates = {days_ago(5)}
        assert compute_current_streak(dates, today=TODAY) == 0


class TestLongestStreak:
    def test_empty_history_is_zero(self):
        assert compute_longest_streak(set()) == 0

    def test_single_day_is_one(self):
        assert compute_longest_streak({TODAY}) == 1

    def test_finds_longest_run_even_if_not_current(self):
        # A 3-day run far in the past, a 1-day run today.
        dates = {days_ago(30), days_ago(29), days_ago(28), TODAY}
        assert compute_longest_streak(dates) == 3

    def test_all_consecutive(self):
        dates = {days_ago(i) for i in range(10)}
        assert compute_longest_streak(dates) == 10


class TestCompletionRate:
    def test_zero_window_is_zero(self):
        assert compute_completion_rate({TODAY}, window_days=0, today=TODAY) == 0.0

    def test_full_completion_in_window(self):
        dates = {days_ago(i) for i in range(7)}
        assert compute_completion_rate(dates, window_days=7, today=TODAY) == 1.0

    def test_partial_completion(self):
        dates = {TODAY, days_ago(1), days_ago(2)}  # 3 of 7 days
        rate = compute_completion_rate(dates, window_days=7, today=TODAY)
        assert rate == pytest.approx(3 / 7, abs=1e-4)

    def test_ignores_completions_outside_the_window(self):
        dates = {days_ago(100)}
        assert compute_completion_rate(dates, window_days=7, today=TODAY) == 0.0
