"""Streak and completion-rate calculations for the Habit Loop.

Twin Intelligence Core — Etappe 3.

Pure functions — no database access, no I/O. Callers (routers/services) fetch
the raw `entry_date`/`completed` rows and pass them in. This keeps the actual
streak math independently testable and reusable (e.g. later by a
`TwinPatternService`).

All "day" comparisons are done in the user's local calendar day (a plain
`date`, already resolved from `local_date`/`entry_date` + `timezone` by the
caller) — never in server/UTC time, per Etappe 3 §5 "Zeitzone
berücksichtigen".
"""

from __future__ import annotations

from datetime import date, timedelta


def compute_current_streak(completed_dates: set[date], *, today: date) -> int:
    """Consecutive completed days up to and including `today`.

    If today isn't completed yet, the streak still counts backwards from
    yesterday so an in-progress streak isn't shown as broken before the day
    is over (mirrors the existing frontend logic in
    `dashboard-habits.tsx`, now computed server-side).
    """
    if not completed_dates:
        return 0

    cursor = today if today in completed_dates else today - timedelta(days=1)
    streak = 0
    while cursor in completed_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def compute_longest_streak(completed_dates: set[date]) -> int:
    """Longest run of consecutive completed days across all history."""
    if not completed_dates:
        return 0

    longest = 0
    current = 0
    previous_day: date | None = None
    for day in sorted(completed_dates):
        if previous_day is not None and day == previous_day + timedelta(days=1):
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous_day = day
    return longest


def compute_completion_rate(
    completed_dates: set[date], *, window_days: int, today: date
) -> float:
    """Fraction (0.0-1.0) of the last `window_days` days (including today)
    that were completed. A habit created less than `window_days` ago is not
    penalized for days before it existed — callers should pass only
    `completed_dates` that could plausibly have been completed (see
    `habit_service.compute_habit_stats`, which clamps the window to the
    habit's `created_at`)."""
    if window_days <= 0:
        return 0.0

    window_start = today - timedelta(days=window_days - 1)
    completed_in_window = sum(1 for day in completed_dates if window_start <= day <= today)
    return round(completed_in_window / window_days, 4)
