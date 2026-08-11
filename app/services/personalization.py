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

REASON_REPEAT_THRESHOLD = 2
"""Twin Core Phase 6 Part B: a classified rejection reason must recur at
least this many times for the SAME category before it contributes anything
to personalization — a single rejection must never overreact."""

MAX_REASON_PENALTY_BONUS = 2
"""Hard cap on how much the reason-based signal alone may add on top of the
existing base category penalty (Step "noise/abuse protection": cap penalty
impact)."""

ALLOWED_REJECTION_REASON_CATEGORIES = frozenset(
    {"timing_not_suitable", "too_difficult", "not_relevant", "already_doing", "preference_conflict", "other"}
)

_REJECTION_REASON_KEYWORDS: dict[str, tuple[str, ...]] = {
    "timing_not_suitable": ("zeitpunkt", "uhrzeit", "morgens", "abends", "zeitlich", "keine zeit", "tageszeit"),
    "too_difficult": ("zu schwer", "zu anstrengend", "schaffe ich nicht", "zu viel", "überfordert", "zu schwierig"),
    "not_relevant": ("nicht relevant", "betrifft mich nicht", "unwichtig", "interessiert mich nicht"),
    "already_doing": ("mache ich schon", "bereits", "schon dabei", "kenne ich schon", "mach ich bereits"),
    "preference_conflict": ("mag ich nicht", "gefällt mir nicht", "gefaellt mir nicht", "keine lust", "nicht mein ding"),
}
"""Deliberately SMALL, explicit keyword sets — never an LLM, never a fuzzy
match. Any text not matching one of these is classified "other" and never
influences personalization (Part B: "do not invent a category from ambiguous
text")."""


def classify_rejection_reason(reason: str | None) -> str:
    """Deterministic, rule-based classification of a free-text rejection
    reason into one of the small set of safe categories. Never guesses —
    unrecognized/empty text always returns "other", which never affects
    personalization (see `compute_category_penalty`)."""
    if not reason:
        return "other"
    lowered = reason.lower()
    for category, keywords in _REJECTION_REASON_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return "other"


def compute_category_penalty(
    recommendation_history: list[dict[str, object]],
    *,
    decisions_by_recommendation_id: dict[str, dict[str, object]] | None = None,
) -> dict[str, int]:
    """One penalty counter per category: `rejected` recommendations increase
    it, `accepted` ones decrease it. A higher value means "suggest this
    category less often" (§6: "häufig abgelehnte Kategorien reduzieren").

    `decisions_by_recommendation_id` (Twin Core Phase 6 Part B, optional,
    defaults to `None` — 100% backward compatible for every existing caller
    that never passes it) additively classifies each rejection's free-text
    reason (never fed into scoring directly, only its safe category) and
    adds a small, CAPPED bonus penalty once the SAME reason category recurs
    at least `REASON_REPEAT_THRESHOLD` times for a category — a single
    rejection, or an ambiguous ("other") reason, never changes anything.
    """
    penalties: dict[str, int] = {}
    reason_counts: dict[tuple[str, str], int] = {}
    decisions_by_recommendation_id = decisions_by_recommendation_id or {}

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

        if status in UNSUCCESSFUL_STATUSES:
            decision = decisions_by_recommendation_id.get(str(rec.get("id")))
            reason_category = classify_rejection_reason(decision.get("reason") if decision else None)
            if reason_category != "other":
                key = (category, reason_category)
                reason_counts[key] = reason_counts.get(key, 0) + 1

    for (category, _reason_category), count in reason_counts.items():
        if count < REASON_REPEAT_THRESHOLD:
            continue
        bonus = min(count - REASON_REPEAT_THRESHOLD + 1, MAX_REASON_PENALTY_BONUS)
        penalties[category] = penalties.get(category, 0) + bonus

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
