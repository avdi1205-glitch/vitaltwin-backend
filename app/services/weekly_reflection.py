"""Weekly Reflection Loop: a real, data-grounded weekly rollup.

Twin Intelligence Core — Etappe 6 §4.

Pure functions over already-fetched data for a single ISO week — no database
access. Never invents a pattern or a development that isn't backed by an
actual comparison of real numbers (Etappe 6 §4: "Keine Muster erfinden").
Below `MIN_CHECKIN_DAYS_FOR_WEEKLY` check-in days in the week, the whole
result is marked `data_sufficient=False` and every derived section stays
empty — the frontend then shows only the fixed disclaimer sentence, never a
half-confident guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_CHECKIN_DAYS_FOR_WEEKLY = 3

INSUFFICIENT_DATA_SUMMARY = "Noch nicht genügend Daten für einen zuverlässigen Wochenrückblick."

STABLE_ROUTINE_MIN_COMPLETION = 0.8
STRUGGLING_HABIT_MAX_COMPLETION = 0.3

# Minimum week-over-week change to be worth mentioning at all — avoids
# reporting noise as if it were a real development.
METRIC_THRESHOLDS: dict[str, float] = {
    "sleep_hours": 0.5,
    "energy": 0.7,
    "movement_minutes": 10.0,
    "stress": 0.7,
    "mood": 0.7,
}
# Whether a higher value is "better" for that metric (stress is inverted).
METRIC_HIGHER_IS_BETTER: dict[str, bool] = {
    "sleep_hours": True,
    "energy": True,
    "movement_minutes": True,
    "stress": False,
    "mood": True,
}
METRIC_LABELS: dict[str, str] = {
    "sleep_hours": "Schlafdauer",
    "energy": "Energie",
    "movement_minutes": "Bewegung",
    "stress": "Stress",
    "mood": "Stimmung",
}


@dataclass(frozen=True)
class WeeklyReflectionResult:
    data_sufficient: bool
    data_points: int
    summary: str
    positive_developments: list[str] = field(default_factory=list)
    stable_routines: list[str] = field(default_factory=list)
    potential_areas: list[str] = field(default_factory=list)
    goal_progress: list[str] = field(default_factory=list)
    most_helpful_recommendations: list[str] = field(default_factory=list)
    least_helpful_recommendations: list[str] = field(default_factory=list)
    suggestions_next_week: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)


def _average(entries: list[dict[str, object]], field_name: str) -> float | None:
    values = [float(e[field_name]) for e in entries if e.get(field_name) is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _metric_developments(
    this_week_entries: list[dict[str, object]], previous_week_entries: list[dict[str, object]]
) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    potentials: list[str] = []
    for metric, threshold in METRIC_THRESHOLDS.items():
        current = _average(this_week_entries, metric)
        previous = _average(previous_week_entries, metric)
        if current is None or previous is None:
            continue
        delta = current - previous
        if abs(delta) < threshold:
            continue
        higher_is_better = METRIC_HIGHER_IS_BETTER[metric]
        improved = delta > 0 if higher_is_better else delta < 0
        label = METRIC_LABELS[metric]
        note = f"{label}: {round(previous, 1)} → {round(current, 1)} im Vergleich zur Vorwoche."
        (positives if improved else potentials).append(note)
    return positives, potentials


def _stable_and_struggling_habits(habits: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    stable: list[str] = []
    struggling: list[str] = []
    for habit in habits:
        if habit.get("status") != "active":
            continue
        completion = habit.get("completion_rate_7d")
        if not isinstance(completion, (int, float)):
            continue
        name = str(habit.get("name") or "Gewohnheit")
        if completion >= STABLE_ROUTINE_MIN_COMPLETION:
            stable.append(f'"{name}" ({round(completion * 100)}% diese Woche)')
        elif completion <= STRUGGLING_HABIT_MAX_COMPLETION:
            struggling.append(f'"{name}" ({round(completion * 100)}% diese Woche)')
    return stable, struggling


def _goal_progress_notes(goals: list[dict[str, object]]) -> list[str]:
    notes: list[str] = []
    for goal in goals:
        title = str(goal.get("title") or "Ziel")
        status = goal.get("status")
        if status == "active":
            notes.append(f'"{title}" wird weiterhin aktiv verfolgt.')
        elif status == "completed":
            notes.append(f'"{title}" wurde als erreicht markiert.')
    return notes


def _recommendation_feedback_notes(recommendation_history: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    helpful_categories: dict[str, int] = {}
    unhelpful_categories: dict[str, int] = {}
    for rec in recommendation_history:
        category = rec.get("category")
        helpfulness = rec.get("helpfulness")
        if not category or not helpfulness:
            continue
        category = str(category)
        if helpfulness == "helpful":
            helpful_categories[category] = helpful_categories.get(category, 0) + 1
        elif helpfulness == "not_helpful":
            unhelpful_categories[category] = unhelpful_categories.get(category, 0) + 1

    most_helpful = [f'Empfehlungen zu "{cat}"' for cat, _ in sorted(helpful_categories.items(), key=lambda kv: -kv[1])[:2]]
    least_helpful = [
        f'Empfehlungen zu "{cat}"' for cat, _ in sorted(unhelpful_categories.items(), key=lambda kv: -kv[1])[:2]
    ]
    return most_helpful, least_helpful


def _suggestions_from_potentials(potential_areas: list[str], struggling_habits: list[str]) -> list[str]:
    suggestions: list[str] = []
    if potential_areas:
        suggestions.append("Vielleicht hilft dir nächste Woche etwas mehr Fokus auf die Bereiche mit Potenzial.")
    if struggling_habits:
        suggestions.append("Eine kleinere, realistischere Version deiner schwierigsten Gewohnheit könnte leichter fallen.")
    return suggestions


def compute_weekly_reflection(
    *,
    this_week_entries: list[dict[str, object]],
    previous_week_entries: list[dict[str, object]],
    habits: list[dict[str, object]],
    goals: list[dict[str, object]],
    recommendation_history: list[dict[str, object]],
    confirmed_patterns: list[dict[str, object]],
) -> WeeklyReflectionResult:
    data_points = len(this_week_entries)
    if data_points < MIN_CHECKIN_DAYS_FOR_WEEKLY:
        return WeeklyReflectionResult(data_sufficient=False, data_points=data_points, summary=INSUFFICIENT_DATA_SUMMARY)

    positives, potentials = _metric_developments(this_week_entries, previous_week_entries)
    stable_routines, struggling_habits = _stable_and_struggling_habits(habits)
    goal_progress = _goal_progress_notes(goals)
    most_helpful, least_helpful = _recommendation_feedback_notes(recommendation_history)
    suggestions = _suggestions_from_potentials(potentials, struggling_habits)
    pattern_notes = [
        str(p.get("summary"))
        for p in confirmed_patterns
        if p.get("status") == "active" and not p.get("contradicting") and p.get("summary")
    ]

    return WeeklyReflectionResult(
        data_sufficient=True,
        data_points=data_points,
        summary="Dein Wochenrückblick basiert auf deinen eigenen Daten dieser Woche.",
        positive_developments=positives,
        stable_routines=stable_routines,
        potential_areas=potentials + struggling_habits,
        goal_progress=goal_progress,
        most_helpful_recommendations=most_helpful,
        least_helpful_recommendations=least_helpful,
        suggestions_next_week=suggestions,
        patterns=pattern_notes,
    )
