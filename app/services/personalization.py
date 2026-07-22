"""Beta-personalization rules.

Twin Intelligence Core — Etappe 4 §6.

Deliberately simple, rule-based, and fully explainable heuristics over the
user's own recommendation history — **not** a trained ML model. Never claim
an ML model is being trained here; that would misrepresent what this code
does (Constitution: Ehrlichkeit).
"""

from __future__ import annotations

from datetime import date, timedelta

REJECTION_THRESHOLD = 2
REPEAT_COOLDOWN_DAYS = 14
UNSUCCESSFUL_STATUSES = frozenset({"rejected", "skipped"})


def compute_category_penalty(recommendation_history: list[dict[str, object]]) -> dict[str, int]:
    """One penalty counter per category: `rejected` recommendations increase
    it, `accepted` ones decrease it. A higher value means "suggest this
    category less often" (§6: "häufig abgelehnte Kategorien reduzieren")."""
    penalties: dict[str, int] = {}
    for rec in recommendation_history:
        category = rec.get("category")
        if not category:
            continue
        category = str(category)
        status = rec.get("status")
        if status == "rejected":
            penalties[category] = penalties.get(category, 0) + 1
        elif status == "accepted":
            penalties[category] = penalties.get(category, 0) - 1
    return penalties


def should_deprioritize_category(category: str, penalties: dict[str, int]) -> bool:
    return penalties.get(category, 0) >= REJECTION_THRESHOLD


def has_recent_unsuccessful_duplicate(
    draft_category: str,
    draft_action: str,
    past_recommendations: list[dict[str, object]],
    *,
    today: date,
    cooldown_days: int = REPEAT_COOLDOWN_DAYS,
) -> bool:
    """True if the exact same category+action was already suggested within
    the cooldown window and didn't succeed (rejected/skipped, or an outcome/
    feedback that marked it unsuccessful) — §6: "identische erfolglose
    Vorschläge nicht ständig wiederholen"."""
    window_start = today - timedelta(days=cooldown_days)
    for rec in past_recommendations:
        if rec.get("category") != draft_category or rec.get("proposed_action") != draft_action:
            continue
        created_raw = rec.get("created_at")
        if not created_raw:
            continue
        try:
            created_date = date.fromisoformat(str(created_raw)[:10])
        except ValueError:
            continue
        if created_date < window_start:
            continue
        if rec.get("status") in UNSUCCESSFUL_STATUSES:
            return True
        if rec.get("outcome_status") == "not_implemented":
            return True
        if rec.get("helpfulness") == "not_helpful":
            return True
    return False


def matches_preferred_time(reminder_time: str | None, current_hour: int) -> bool:
    """Simple time-of-day match: a habit's `reminder_time` (HH:MM) within 3
    hours of the current hour counts as "preferred time" (§6: "bevorzugte
    Tageszeiten berücksichtigen"). Informational only — never blocks
    generation, only used to annotate priority/explanation."""
    if not reminder_time:
        return False
    try:
        reminder_hour = int(str(reminder_time).split(":")[0])
    except (ValueError, IndexError):
        return False
    return abs(reminder_hour - current_hour) <= 3
