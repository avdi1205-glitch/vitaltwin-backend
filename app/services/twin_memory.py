"""Twin Memory: candidate detection and lifecycle rules.

Twin Intelligence Core — Etappe 5 §1, §2, §5.

Pure functions over already-fetched data (habits with stats, goals,
recommendation history, existing memories/patterns) — no database access, no
randomness, no ML. Mirrors the style of `recommendation_rules.py`
(Etappe 4): every detector either returns a `MemoryCandidate` (or a list of
them) or nothing, never a memory "because we need one".

Core rule (Etappe 5 §1): "Der Twin darf eine einmalige Beobachtung nicht als
absolute Wahrheit speichern." Every detector-produced candidate starts at
`status="candidate"` with a capped confidence — only repeated confirmation
(via `promote_after_observation`) or an explicit user action
(`apply_user_confirmation`) can move it further along the lifecycle in
`routers/twin_memory.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from . import personalization

MIN_CONFIDENCE = 0.05
MAX_CONFIDENCE = 0.95
INITIAL_CANDIDATE_CONFIDENCE = 0.4

CONFIRMATION_CONFIDENCE_STEP = 0.15
CONTRADICTION_CONFIDENCE_STEP = 0.25

# How many independent times the same candidate must be (re-)observed before
# it is promoted from "candidate" to "active" without the user having to do
# anything — see Etappe 5 §5 "wiederholte Bestätigung".
REPEATED_OBSERVATION_THRESHOLD = 3

SUCCESSFUL_ROUTINE_MIN_COMPLETION_RATE = 0.8
SUCCESSFUL_ROUTINE_MIN_STREAK = 7
PREFERRED_TIME_MIN_COMPLETION_RATE = 0.7
LONG_TERM_GOAL_MIN_DAYS = 30
CONFIRMED_PREFERENCE_MIN_ACCEPTED = 2

USABLE_STATUSES = frozenset({"active", "confirmed"})
"""Only memories in these statuses may be used for recommendations/AI
context (Etappe 5 §2: gelöschte/abgelehnte Memories dürfen nicht mehr
verwendet werden)."""


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: str
    memory_key: str
    title: str
    normalized_value: dict[str, object]
    human_readable_value: str
    source: str
    source_references: tuple[str, ...]
    confidence: float = INITIAL_CANDIDATE_CONFIDENCE


def _clamp(value: float) -> float:
    return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, value))


def bump_confidence(confidence: float | None, *, step: float = CONFIRMATION_CONFIDENCE_STEP) -> float:
    base = confidence if confidence is not None else INITIAL_CANDIDATE_CONFIDENCE
    return _clamp(base + step)


def decay_confidence(confidence: float | None, *, step: float = CONTRADICTION_CONFIDENCE_STEP) -> float:
    base = confidence if confidence is not None else INITIAL_CANDIDATE_CONFIDENCE
    return _clamp(base - step)


def is_usable_for_recommendations(status: str) -> bool:
    return status in USABLE_STATUSES


def is_expired(expires_at: str | None, *, now: datetime) -> bool:
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed < now


def promote_after_observation(current_status: str, *, observation_count: int) -> str:
    """`candidate` -> `active` once observed independently at least
    `REPEATED_OBSERVATION_THRESHOLD` times. Anything already further along
    the lifecycle (`confirmed`, `disputed`, `archived`, `deleted`) is left
    untouched — repeated automatic detection must never override an
    explicit user decision."""
    if current_status == "candidate" and observation_count >= REPEATED_OBSERVATION_THRESHOLD:
        return "active"
    return current_status


def apply_user_confirmation(memory: dict[str, object], *, now: datetime) -> dict[str, object]:
    """Etappe 5 §2 "Memory bestätigen". An explicit user confirmation always
    moves straight to `confirmed`, regardless of how many times it was
    observed automatically."""
    return {
        "status": "confirmed",
        "user_confirmed": True,
        "confidence": bump_confidence(memory.get("confidence")),
        "last_confirmed_at": now.isoformat(),
    }


def apply_user_correction(
    memory: dict[str, object], *, human_readable_value: str, normalized_value: dict[str, object] | None, now: datetime
) -> dict[str, object]:
    """Etappe 5 §2 "Memory korrigieren". Treated as an implicit confirmation
    of the corrected value (the user cared enough to fix it, not just
    dismiss it) — status moves to `confirmed`, but a fresh correction resets
    trust to a moderate level rather than fully re-bumping, since the
    original observation was wrong."""
    updates: dict[str, object] = {
        "human_readable_value": human_readable_value,
        "status": "confirmed",
        "user_confirmed": True,
        "confidence": _clamp(0.6),
        "last_confirmed_at": now.isoformat(),
    }
    if normalized_value is not None:
        updates["normalized_value"] = normalized_value
    return updates


def apply_user_rejection(memory: dict[str, object], *, now: datetime) -> dict[str, object]:
    """Etappe 5 §2 "Memory ablehnen". Marks the memory `disputed` (contested,
    not yet removed) and lowers its confidence sharply — a disputed memory is
    excluded from recommendations/AI context (`is_usable_for_recommendations`)
    but stays visible to the user until they archive or delete it."""
    return {
        "status": "disputed",
        "user_confirmed": False,
        "confidence": decay_confidence(memory.get("confidence")),
        "updated_at": now.isoformat(),
    }


def apply_archive(now: datetime) -> dict[str, object]:
    return {"status": "archived", "updated_at": now.isoformat()}


def apply_deletion(now: datetime) -> dict[str, object]:
    return {"status": "deleted", "deleted_at": now.isoformat(), "updated_at": now.isoformat()}


def reevaluate_dependent_candidates(
    deleted_memory_type: str, other_candidates: list[dict[str, object]], *, now: datetime
) -> list[tuple[str, dict[str, object]]]:
    """Etappe 5 §2: "abhängige Kandidaten neu bewerten" after a deletion.
    Any still-unconfirmed candidate/active memory of the *same* type as the
    just-deleted one had its evidentiary basis called into question (the
    user just said "that's not right" about a related memory) — so its
    confidence is reduced and it is pushed back to `candidate` for
    re-confirmation rather than left `active` on stale trust. Confirmed
    memories (the user explicitly vouched for them) are left untouched."""
    updates: list[tuple[str, dict[str, object]]] = []
    for candidate in other_candidates:
        if candidate.get("memory_type") != deleted_memory_type:
            continue
        if candidate.get("status") not in ("candidate", "active"):
            continue
        updates.append(
            (
                str(candidate.get("id")),
                {
                    "status": "candidate",
                    "confidence": decay_confidence(candidate.get("confidence"), step=0.1),
                    "updated_at": now.isoformat(),
                },
            )
        )
    return updates


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def detect_preferred_activity_time(habits: list[dict[str, object]]) -> list[MemoryCandidate]:
    """Habit mit fester Erinnerungszeit + hoher Erfüllungsquote ->
    "bevorzugte Aktivitätszeit"."""
    candidates: list[MemoryCandidate] = []
    for habit in habits:
        reminder_time = habit.get("reminder_time")
        completion = habit.get("completion_rate_30d")
        if not reminder_time or not isinstance(completion, (int, float)):
            continue
        if completion < PREFERRED_TIME_MIN_COMPLETION_RATE:
            continue
        name = str(habit.get("name") or "deine Gewohnheit")
        candidates.append(
            MemoryCandidate(
                memory_type="bevorzugte_aktivitaetszeit",
                memory_key=f"preferred_time:{habit.get('id')}",
                title=f'Bevorzugte Zeit für "{name}"',
                normalized_value={"habit_id": habit.get("id"), "reminder_time": reminder_time},
                human_readable_value=f'Du erledigst "{name}" meist erfolgreich um {reminder_time} Uhr.',
                source="calculated",
                source_references=("completion_rate_30d", "reminder_time"),
                confidence=min(MAX_CONFIDENCE, INITIAL_CANDIDATE_CONFIDENCE + completion / 4),
            )
        )
    return candidates


def detect_successful_routine(habits: list[dict[str, object]]) -> list[MemoryCandidate]:
    """Sehr hohe Erfüllungsquote + langer Streak -> "erfolgreiche Routine"."""
    candidates: list[MemoryCandidate] = []
    for habit in habits:
        completion = habit.get("completion_rate_30d")
        streak = habit.get("longest_streak")
        if not isinstance(completion, (int, float)) or not isinstance(streak, int):
            continue
        if completion < SUCCESSFUL_ROUTINE_MIN_COMPLETION_RATE or streak < SUCCESSFUL_ROUTINE_MIN_STREAK:
            continue
        name = str(habit.get("name") or "deine Gewohnheit")
        candidates.append(
            MemoryCandidate(
                memory_type="erfolgreiche_routine",
                memory_key=f"successful_routine:{habit.get('id')}",
                title=f'Erfolgreiche Routine: "{name}"',
                normalized_value={"habit_id": habit.get("id"), "longest_streak": streak},
                human_readable_value=f'"{name}" funktioniert bei dir sehr zuverlässig (Serie: {streak} Tage).',
                source="calculated",
                source_references=("completion_rate_30d", "longest_streak"),
                confidence=min(MAX_CONFIDENCE, INITIAL_CANDIDATE_CONFIDENCE + completion / 4),
            )
        )
    return candidates


def detect_rejected_recommendation_type(recommendation_history: list[dict[str, object]]) -> list[MemoryCandidate]:
    """Wiederholt abgelehnte Empfehlungskategorie -> "regelmäßig abgelehnter
    Empfehlungstyp" (wiederverwendet den Etappe-4-Kategorien-Malus)."""
    penalties = personalization.compute_category_penalty(recommendation_history)
    candidates: list[MemoryCandidate] = []
    for category, penalty in penalties.items():
        if not personalization.should_deprioritize_category(category, penalties):
            continue
        candidates.append(
            MemoryCandidate(
                memory_type="abgelehnter_empfehlungstyp",
                memory_key=f"rejected_category:{category}",
                title=f'Empfehlungen zu "{category}" werden meist abgelehnt',
                normalized_value={"category": category, "penalty": penalty},
                human_readable_value=f'Du hast Empfehlungen zu "{category}" wiederholt abgelehnt.',
                source="calculated",
                source_references=("recommendation_decisions",),
                confidence=min(MAX_CONFIDENCE, INITIAL_CANDIDATE_CONFIDENCE + 0.1 * penalty),
            )
        )
    return candidates


def detect_confirmed_preference(recommendation_history: list[dict[str, object]]) -> list[MemoryCandidate]:
    """Wiederholt angenommene Empfehlungskategorie -> "bestätigte
    Nutzerpräferenz"."""
    accepted_counts: dict[str, int] = {}
    rejected_categories: set[str] = set()
    for rec in recommendation_history:
        category = rec.get("category")
        if not category:
            continue
        category = str(category)
        status = rec.get("status")
        if status == "accepted":
            accepted_counts[category] = accepted_counts.get(category, 0) + 1
        elif status == "rejected":
            rejected_categories.add(category)

    candidates: list[MemoryCandidate] = []
    for category, count in accepted_counts.items():
        if count < CONFIRMED_PREFERENCE_MIN_ACCEPTED or category in rejected_categories:
            continue
        candidates.append(
            MemoryCandidate(
                memory_type="bestaetigte_praeferenz",
                memory_key=f"confirmed_preference:{category}",
                title=f'Bevorzugt Empfehlungen zu "{category}"',
                normalized_value={"category": category, "accepted_count": count},
                human_readable_value=f'Du nimmst Empfehlungen zu "{category}" meist an.',
                source="calculated",
                source_references=("recommendation_decisions",),
                confidence=min(MAX_CONFIDENCE, INITIAL_CANDIDATE_CONFIDENCE + 0.1 * count),
            )
        )
    return candidates


def detect_active_long_term_goal(goals: list[dict[str, object]], *, today: date) -> list[MemoryCandidate]:
    """Aktives Ziel ohne Zieldatum oder mit weit entferntem Zieldatum ->
    "aktives langfristiges Ziel"."""
    candidates: list[MemoryCandidate] = []
    for goal in goals:
        if goal.get("status") != "active":
            continue
        target_date_raw = goal.get("target_date")
        is_long_term = target_date_raw is None
        if target_date_raw:
            try:
                target_date = date.fromisoformat(str(target_date_raw))
                is_long_term = (target_date - today).days >= LONG_TERM_GOAL_MIN_DAYS
            except ValueError:
                is_long_term = False
        if not is_long_term:
            continue
        title = str(goal.get("title") or "dein Ziel")
        candidates.append(
            MemoryCandidate(
                memory_type="aktives_langfristiges_ziel",
                memory_key=f"long_term_goal:{goal.get('id')}",
                title=f'Langfristiges Ziel: "{title}"',
                normalized_value={"goal_id": goal.get("id"), "goal_type": goal.get("goal_type")},
                human_readable_value=f'Du verfolgst aktuell das langfristige Ziel "{title}".',
                source="calculated",
                source_references=("wellness_goal",),
                confidence=INITIAL_CANDIDATE_CONFIDENCE,
            )
        )
    return candidates


def promote_pattern_to_memory(pattern: dict[str, object], *, threshold: float = 0.7) -> MemoryCandidate | None:
    """Etappe 5 §1 "bestätigtes langfristiges Muster": ein wiederholt
    erkanntes, nicht widersprüchliches Pattern wird zu einer Memory. Nur
    Patterns mit ausreichend hoher Konfidenz und ohne widersprüchliche Daten
    qualifizieren sich — siehe `pattern_detection.py`."""
    confidence = pattern.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < threshold:
        return None
    if pattern.get("contradicting"):
        return None
    summary = str(pattern.get("summary") or "")
    pattern_key = str(pattern.get("pattern_key") or pattern.get("id") or "pattern")
    return MemoryCandidate(
        memory_type="bestaetigtes_muster",
        memory_key=f"confirmed_pattern:{pattern_key}",
        title=str(pattern.get("pattern_type") or "Erkanntes Muster"),
        normalized_value={"pattern_id": pattern.get("id"), "variables": pattern.get("variables")},
        human_readable_value=summary,
        source="calculated",
        source_references=("twin_pattern",),
        confidence=float(confidence),
    )


def generate_memory_candidates(
    *,
    habits: list[dict[str, object]],
    goals: list[dict[str, object]],
    recommendation_history: list[dict[str, object]],
    confirmed_patterns: list[dict[str, object]],
    today: date,
) -> list[MemoryCandidate]:
    """Runs every detector and collects all resulting candidates. Callers
    (`routers/twin_memory.py`) de-duplicate against existing memories
    (`memory_key`) before persisting anything."""
    candidates: list[MemoryCandidate] = []
    candidates.extend(detect_preferred_activity_time(habits))
    candidates.extend(detect_successful_routine(habits))
    candidates.extend(detect_rejected_recommendation_type(recommendation_history))
    candidates.extend(detect_confirmed_preference(recommendation_history))
    candidates.extend(detect_active_long_term_goal(goals, today=today))
    for pattern in confirmed_patterns:
        promoted = promote_pattern_to_memory(pattern)
        if promoted is not None:
            candidates.append(promoted)
    return candidates
