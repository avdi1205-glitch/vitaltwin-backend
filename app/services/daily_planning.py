"""Daily Planning Loop: prioritized daily-plan action generation.

Twin Intelligence Core — Etappe 6 §1-2.

Pure functions over already-fetched data (goals, habits with stats, active
recommendations, yesterday's plan actions, confirmed "preferred activity
time" memories) — no database access, no ML, no randomness. Mirrors the
style of `recommendation_rules.py`/`twin_memory.py`.

Prioritization considers exactly the nine factors from Etappe 6 §1: aktive
Ziele, offene Gewohnheiten, aktueller Check-in (indirekt über bereits
gefilterte `recommendations`, die selbst check-in-basiert sind, siehe
Etappe 4), bisherige Erfolge (`completion_rate_7d`), Nutzerfeedback (bereits
in `recommendations` durch die Etappe-4-Personalisierung berücksichtigt),
bevorzugte Tageszeit (`matches_preferred_time`), Datenqualität (Kandidaten
aus wenig belastbaren Daten werden nicht höher gewichtet), Plan des Vortags
(`carried_over`-Bonus für noch offene Aktionen), aktive bestätigte Memories
(Bonus, wenn eine Gewohnheit eine bestätigte "bevorzugte Aktivitätszeit"-
Memory hat).

Jede erzeugte Aktion trägt ihre eigene, für den Nutzer verständliche
Begründung (`reasoning`) und einen groben Aufwand (`estimated_effort`) —
nie nur einen internen Score.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import personalization

MAX_DAILY_PLAN_ACTIONS = 3

CARRIED_OVER_BONUS = 2.0
PREFERRED_TIME_BONUS = 2.0
GOAL_BASE_SCORE = 5.0
HABIT_BASE_SCORE = 4.0
RECOMMENDATION_PRIORITY_BONUS = {"high": 3.0, "medium": 1.0, "low": 0.0}

HIGH_SCORE_THRESHOLD = 8.0
MEDIUM_SCORE_THRESHOLD = 4.0

DEFAULT_ESTIMATED_EFFORT = "kurz (5-10 Minuten)"


@dataclass(frozen=True)
class PlannedActionDraft:
    description: str
    reasoning: str
    estimated_effort: str
    priority: str  # "low" | "medium" | "high"
    source: str  # "goal" | "habit" | "recommendation" | "carried_over"
    score: float
    goal_id: str | None = None
    habit_id: str | None = None
    recommendation_id: str | None = None
    carried_over: bool = False


def _priority_from_score(score: float) -> str:
    if score >= HIGH_SCORE_THRESHOLD:
        return "high"
    if score >= MEDIUM_SCORE_THRESHOLD:
        return "medium"
    return "low"


def next_status_after_adjustment(current_status: str) -> str:
    """Etappe 6 §1 "Anpassungsmöglichkeit": adjusting the text of a still
    undecided (`proposed`) action counts as the user's decision to keep it,
    just modified — mirrors the Recommendation Loop's `modified` status
    (Etappe 4). Adjusting an already-decided action (`accepted`/`completed`/
    ...) only updates its text, the status stays whatever it already was."""
    return "modified" if current_status == "proposed" else current_status


def _goal_candidates(goals: list[dict[str, object]]) -> list[PlannedActionDraft]:
    candidates: list[PlannedActionDraft] = []
    for goal in goals:
        if goal.get("status") != "active":
            continue
        title = str(goal.get("title") or "dein Ziel")
        candidates.append(
            PlannedActionDraft(
                description=f'Ein kleiner Schritt für dein Ziel "{title}"',
                reasoning=f'Du verfolgst aktuell das aktive Ziel "{title}".',
                estimated_effort="10-15 Minuten",
                priority=_priority_from_score(GOAL_BASE_SCORE),
                source="goal",
                score=GOAL_BASE_SCORE,
                goal_id=str(goal.get("id")) if goal.get("id") else None,
            )
        )
    return candidates


def _habit_candidates(
    habits: list[dict[str, object]], *, current_hour: int, preferred_time_habit_ids: set[str]
) -> list[PlannedActionDraft]:
    candidates: list[PlannedActionDraft] = []
    for habit in habits:
        if habit.get("status") != "active" or habit.get("completed_today"):
            continue
        habit_id = str(habit.get("id")) if habit.get("id") else None
        name = str(habit.get("name") or "deine Gewohnheit")
        completion = habit.get("completion_rate_7d")
        completion = float(completion) if isinstance(completion, (int, float)) else 0.0

        score = HABIT_BASE_SCORE + (1 - completion) * 3
        time_note = ""
        reminder_time = habit.get("reminder_time")
        if reminder_time and personalization.matches_preferred_time(str(reminder_time), current_hour):
            score += PREFERRED_TIME_BONUS
            time_note = f" Jetzt ist etwa deine übliche Zeit dafür ({reminder_time} Uhr)."
        if habit_id and habit_id in preferred_time_habit_ids:
            score += PREFERRED_TIME_BONUS

        reasoning = f'"{name}" ist diese Woche zu {round(completion * 100)}% erledigt.{time_note}'
        candidates.append(
            PlannedActionDraft(
                description=f'"{name}" heute umsetzen',
                reasoning=reasoning,
                estimated_effort=str(habit.get("target") or DEFAULT_ESTIMATED_EFFORT),
                priority=_priority_from_score(score),
                source="habit",
                score=score,
                habit_id=habit_id,
            )
        )
    return candidates


def _recommendation_candidates(recommendations: list[dict[str, object]]) -> list[PlannedActionDraft]:
    candidates: list[PlannedActionDraft] = []
    for rec in recommendations:
        if rec.get("status") != "proposed":
            continue
        confidence = rec.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.5
        priority_bonus = RECOMMENDATION_PRIORITY_BONUS.get(str(rec.get("priority")), 0.0)
        score = confidence * 10 * 0.5 + priority_bonus
        candidates.append(
            PlannedActionDraft(
                description=str(rec.get("proposed_action") or rec.get("title") or "Empfehlung umsetzen"),
                reasoning=f'Dein Twin empfiehlt das aktuell: "{rec.get("title")}".',
                estimated_effort=DEFAULT_ESTIMATED_EFFORT,
                priority=_priority_from_score(score),
                source="recommendation",
                score=score,
                goal_id=str(rec["goal_id"]) if rec.get("goal_id") else None,
                habit_id=str(rec["habit_id"]) if rec.get("habit_id") else None,
                recommendation_id=str(rec.get("id")) if rec.get("id") else None,
            )
        )
    return candidates


def _carried_over_candidates(yesterday_actions: list[dict[str, object]]) -> list[PlannedActionDraft]:
    candidates: list[PlannedActionDraft] = []
    for action in yesterday_actions:
        if action.get("status") not in ("proposed", "accepted", "modified"):
            continue
        description = str(action.get("user_adjusted_description") or action.get("description") or "")
        if not description:
            continue
        candidates.append(
            PlannedActionDraft(
                description=description,
                reasoning="Von gestern noch offen — heute nachholen?",
                estimated_effort=str(action.get("estimated_effort") or DEFAULT_ESTIMATED_EFFORT),
                priority="medium",
                source="carried_over",
                score=HABIT_BASE_SCORE + CARRIED_OVER_BONUS,
                goal_id=str(action["goal_id"]) if action.get("goal_id") else None,
                habit_id=str(action["habit_id"]) if action.get("habit_id") else None,
                recommendation_id=str(action["recommendation_id"]) if action.get("recommendation_id") else None,
                carried_over=True,
            )
        )
    return candidates


def generate_daily_plan_actions(
    *,
    goals: list[dict[str, object]],
    habits: list[dict[str, object]],
    recommendations: list[dict[str, object]],
    yesterday_actions: list[dict[str, object]],
    preferred_time_habit_ids: set[str] | None = None,
    current_hour: int,
) -> list[PlannedActionDraft]:
    """Collects candidates from every source, sorts by score, and returns at
    most `MAX_DAILY_PLAN_ACTIONS` — "keine Informationsflut" (Etappe 6 §1).
    Carried-over actions from yesterday are deduplicated against fresh
    goal/habit candidates referencing the same goal/habit (no doubled-up
    nudge for the same thing)."""
    preferred_time_habit_ids = preferred_time_habit_ids or set()

    carried_over = _carried_over_candidates(yesterday_actions)
    carried_over_goal_ids = {c.goal_id for c in carried_over if c.goal_id}
    carried_over_habit_ids = {c.habit_id for c in carried_over if c.habit_id}

    candidates: list[PlannedActionDraft] = list(carried_over)
    for candidate in _goal_candidates(goals):
        if candidate.goal_id in carried_over_goal_ids:
            continue
        candidates.append(candidate)
    for candidate in _habit_candidates(habits, current_hour=current_hour, preferred_time_habit_ids=preferred_time_habit_ids):
        if candidate.habit_id in carried_over_habit_ids:
            continue
        candidates.append(candidate)
    candidates.extend(_recommendation_candidates(recommendations))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:MAX_DAILY_PLAN_ACTIONS]
