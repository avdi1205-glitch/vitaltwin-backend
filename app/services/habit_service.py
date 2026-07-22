"""Habit Loop service: streak/completion-rate computation wired to real
`vt_habit_entries` rows.

Twin Intelligence Core — Etappe 3.

Keeps `routers/profile.py` free of date-math — it fetches rows and calls
`compute_habit_stats`, which stays independently unit-testable (see
`tests/test_habit_service.py`).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TypedDict

from .streaks import compute_completion_rate, compute_current_streak, compute_longest_streak


class HabitStats(TypedDict):
    completed_today: bool
    current_streak: int
    longest_streak: int
    completion_rate_7d: float
    completion_rate_30d: float


def _parse_entry_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def compute_habit_stats(
    entries: list[dict[str, object]],
    *,
    habit_created_at: str | None,
    today: date,
) -> HabitStats:
    """`entries` are the raw `vt_habit_entries` rows for a single habit
    (already scoped to the owning user server-side). Only rows with
    `completed = true` count towards a streak/completion day."""
    completed_dates: set[date] = set()
    for entry in entries:
        if not entry.get("completed"):
            continue
        parsed = _parse_entry_date(entry.get("entry_date"))
        if parsed is not None:
            completed_dates.add(parsed)

    # A habit can't have been completed before it existed — clamp the
    # completion-rate window so a brand-new habit isn't unfairly shown as
    # "0% this month" on day one.
    created_date: date | None = None
    if habit_created_at:
        try:
            created_date = datetime.fromisoformat(str(habit_created_at).replace("Z", "+00:00")).date()
        except ValueError:
            created_date = None

    def _window_for(days: int) -> int:
        if created_date is None:
            return days
        days_since_creation = (today - created_date).days + 1
        return max(1, min(days, days_since_creation))

    return {
        "completed_today": today in completed_dates,
        "current_streak": compute_current_streak(completed_dates, today=today),
        "longest_streak": compute_longest_streak(completed_dates),
        "completion_rate_7d": compute_completion_rate(completed_dates, window_days=_window_for(7), today=today),
        "completion_rate_30d": compute_completion_rate(completed_dates, window_days=_window_for(30), today=today),
    }
