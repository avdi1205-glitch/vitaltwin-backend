"""Rule-based recommendation generation.

Twin Intelligence Core — Etappe 4 §2 ("regelbasierte Beta-Empfehlungen").

Pure functions over already-fetched data (check-in entries, habits, goals) —
no database access, no randomness. Each rule either returns a
`RecommendationDraft` or `None`/an empty list if there isn't enough data or
the condition doesn't hold — never a recommendation "because we need one".

Every draft carries exactly the data it was based on (`data_used`,
`period_days`, `data_points`, `data_quality`) so the Explainability endpoint
(`services/explainability.py`) never has to invent a justification after the
fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MIN_DATA_POINTS = 3
LOOKBACK_DAYS = 7

SHORT_SLEEP_HOURS = 6.5
SHORT_SLEEP_MIN_OCCURRENCES = 3

LOW_MOVEMENT_MINUTES = 20.0
HIGH_STRESS_THRESHOLD = 7.0
LOW_COMPLETION_RATE = 0.5


@dataclass(frozen=True)
class RecommendationDraft:
    category: str
    title: str
    proposed_action: str
    priority: str  # "low" | "medium" | "high"
    rule_name: str
    data_used: tuple[str, ...]
    period_days: int
    data_points: int
    data_quality: str  # see core/validation.py::DataQuality
    confidence: float
    expected_benefit: str
    goal_id: str | None = None
    habit_id: str | None = None


def _recent_values(entries: list[dict[str, object]], field_name: str, *, today: date, days: int) -> list[float]:
    window_start = today - timedelta(days=days - 1)
    values: list[float] = []
    for entry in entries:
        raw_date = entry.get("entry_date")
        if not raw_date:
            continue
        entry_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        if not (window_start <= entry_date <= today):
            continue
        value = entry.get(field_name)
        if value is not None:
            values.append(float(value))
    return values


def evaluate_sleep_rule(entries: list[dict[str, object]], *, today: date) -> RecommendationDraft | None:
    """Wiederholt kurzer Schlaf -> kleine Abendroutine vorschlagen."""
    values = _recent_values(entries, "sleep_hours", today=today, days=LOOKBACK_DAYS)
    if len(values) < MIN_DATA_POINTS:
        return None
    short_nights = sum(1 for v in values if v < SHORT_SLEEP_HOURS)
    if short_nights < SHORT_SLEEP_MIN_OCCURRENCES:
        return None
    return RecommendationDraft(
        category="schlaf",
        title="Kleine Abendroutine für besseren Schlaf",
        proposed_action="Heute 30 Minuten vor dem Schlafengehen Bildschirme weglegen und eine feste Einschlafzeit einhalten.",
        priority="medium",
        rule_name="repeated_short_sleep",
        data_used=("sleep_hours",),
        period_days=LOOKBACK_DAYS,
        data_points=len(values),
        data_quality="calculated" if len(values) >= 4 else "partial",
        confidence=min(0.9, 0.5 + 0.1 * short_nights),
        expected_benefit="Mögliche Verbesserung von Schlafdauer und -qualität.",
    )


def evaluate_movement_rule(entries: list[dict[str, object]], *, today: date) -> RecommendationDraft | None:
    """Geringe Bewegung -> realistische kleine Bewegungseinheit vorschlagen."""
    values = _recent_values(entries, "movement_minutes", today=today, days=LOOKBACK_DAYS)
    if len(values) < MIN_DATA_POINTS:
        return None
    average = sum(values) / len(values)
    if average >= LOW_MOVEMENT_MINUTES:
        return None
    return RecommendationDraft(
        category="bewegung",
        title="Kleine Bewegungseinheit einplanen",
        proposed_action="Ein 10-15-minütiger Spaziergang heute kann schon einen Unterschied machen.",
        priority="medium",
        rule_name="low_movement_average",
        data_used=("movement_minutes",),
        period_days=LOOKBACK_DAYS,
        data_points=len(values),
        data_quality="calculated" if len(values) >= 4 else "partial",
        confidence=0.6,
        expected_benefit="Mehr Bewegung im Alltag, realistisch dosiert.",
    )


def evaluate_stress_rule(entries: list[dict[str, object]], *, today: date) -> RecommendationDraft | None:
    """Hoher Stress -> kurze Wellness-Pause vorschlagen."""
    values = _recent_values(entries, "stress", today=today, days=LOOKBACK_DAYS)
    if len(values) < MIN_DATA_POINTS:
        return None
    average = sum(values) / len(values)
    if average < HIGH_STRESS_THRESHOLD:
        return None
    return RecommendationDraft(
        category="stress",
        title="Kurze Wellness-Pause",
        proposed_action="Nimm dir heute 5 Minuten für eine bewusste Atempause oder einen kurzen Spaziergang ohne Handy.",
        priority="high",
        rule_name="high_stress_average",
        data_used=("stress",),
        period_days=LOOKBACK_DAYS,
        data_points=len(values),
        data_quality="calculated" if len(values) >= 4 else "partial",
        confidence=0.65,
        expected_benefit="Möglicher kurzfristiger Stressabbau.",
    )


def evaluate_habit_rule(habits: list[dict[str, object]]) -> list[RecommendationDraft]:
    """Offene Gewohnheit -> passende Erinnerung/kleinere Aktion.

    `habits` entries are expected to already include the computed stats from
    `services/habit_service.py` (`completed_today`, `completion_rate_7d`).
    """
    drafts: list[RecommendationDraft] = []
    for habit in habits:
        if habit.get("status") != "active":
            continue
        if habit.get("completed_today"):
            continue
        completion = habit.get("completion_rate_7d")
        if not isinstance(completion, (int, float)) or completion >= LOW_COMPLETION_RATE:
            continue
        name = str(habit.get("name") or "deine Gewohnheit")
        habit_id = habit.get("id")
        drafts.append(
            RecommendationDraft(
                category=str(habit.get("category") or "sonstiges"),
                title=f'Erinnerung: "{name}"',
                proposed_action=f'Nimm dir heute kurz Zeit für "{name}".',
                priority="low",
                rule_name="open_habit_low_completion",
                data_used=("habit_entries",),
                period_days=LOOKBACK_DAYS,
                data_points=1,
                data_quality="calculated",
                confidence=0.5,
                expected_benefit="Unterstützt den Aufbau dieser Gewohnheit.",
                habit_id=str(habit_id) if habit_id else None,
            )
        )
    return drafts


def evaluate_goal_rule(goals: list[dict[str, object]]) -> list[RecommendationDraft]:
    """Aktives Ziel -> relevante Aktion priorisieren."""
    drafts: list[RecommendationDraft] = []
    for goal in goals:
        if goal.get("status") != "active":
            continue
        title = str(goal.get("title") or "dein Ziel")
        goal_id = goal.get("id")
        drafts.append(
            RecommendationDraft(
                category=str(goal.get("goal_type") or "eigenes_ziel"),
                title=f'Fokus auf dein Ziel: "{title}"',
                proposed_action="Plane heute einen kleinen, konkreten Schritt für dieses Ziel ein.",
                priority="medium",
                rule_name="active_goal_focus",
                data_used=("wellness_goal",),
                period_days=0,
                data_points=1,
                data_quality="calculated",
                confidence=0.5,
                expected_benefit="Hält dein aktives Ziel im Alltag präsent.",
                goal_id=str(goal_id) if goal_id else None,
            )
        )
    return drafts


def generate_recommendations(
    *,
    daily_entries: list[dict[str, object]],
    habits: list[dict[str, object]],
    goals: list[dict[str, object]],
    today: date,
) -> list[RecommendationDraft]:
    """Runs every rule and collects all resulting drafts. Callers
    (`routers/recommendations.py`) apply personalization filtering and
    de-duplication before persisting anything."""
    drafts: list[RecommendationDraft] = []
    for rule in (evaluate_sleep_rule, evaluate_movement_rule, evaluate_stress_rule):
        draft = rule(daily_entries, today=today)
        if draft is not None:
            drafts.append(draft)
    drafts.extend(evaluate_habit_rule(habits))
    drafts.extend(evaluate_goal_rule(goals))
    return drafts
