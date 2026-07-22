"""Recommendation, Decision, Outcome, and Feedback loops.

Twin Intelligence Core — Etappe 4.

Endpoints (mounted at `/api/recommendations` in `app/main.py`):

- `GET  /`                        list active recommendations, generating new
                                   rule-based ones from the user's own recent
                                   data if warranted (§1-2).
- `POST /{id}/decision`           accept / modify / skip / reject (§3).
- `POST /{id}/outcome`            report how it went (§4).
- `POST /{id}/feedback`           rate helpfulness (§5).
- `GET  /{id}/why`                explainability (§7).

Nutzertrennung: every endpoint resolves `email` server-side from the session
token (`core/auth.py`) and every recommendation lookup is scoped
`.eq("email", email)` — a manipulated/guessed id can never return another
user's recommendation (404, not 403, see `core/auth.py`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.audit import record_audit_event
from ..core.auth import require_email as _require_email_dependency
from ..core.learning_events import record_learning_event
from ..core.supabase import supabase
from ..core.validation import MAX_FEEDBACK_COMMENT, validate_short_text
from ..services import personalization
from ..services.explainability import build_explanation_response
from ..services.habit_service import compute_habit_stats
from ..services.recommendation_rules import RecommendationDraft, generate_recommendations

router = APIRouter()

RECOMMENDATION_TABLE = "vt_recommendations"
DECISION_TABLE = "vt_recommendation_decisions"
OUTCOME_TABLE = "vt_recommendation_outcomes"
FEEDBACK_TABLE = "vt_recommendation_feedback"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
HABIT_TABLE = "vt_habits"
HABIT_ENTRY_TABLE = "vt_habit_entries"
GOAL_TABLE = "vt_wellness_goals"

ALLOWED_DECISIONS = {"accepted", "modified", "skipped", "rejected"}
ALLOWED_OUTCOME_STATUSES = {"not_started", "started", "partially_completed", "completed", "not_implemented"}
ALLOWED_OUTCOME_SOURCES = {"user_reported", "derived_from_checkin", "derived_from_habit_entry", "imported_from_wearable"}
ALLOWED_HELPFULNESS = {"helpful", "partially_helpful", "not_helpful"}
ALLOWED_FEEDBACK_REASONS = {
    "nicht_passend",
    "falscher_zeitpunkt",
    "zu_schwierig",
    "zu_einfach",
    "bereits_erledigt",
    "unverstaendlich",
    "nicht_relevant",
    "anderer_grund",
}

RECOMMENDATION_VALIDITY_DAYS = 3
ACTIVE_STATUSES = ("proposed", "accepted", "modified")


def _require_email(authorization: str | None) -> str:
    return _require_email_dependency(authorization)


class DecisionInput(BaseModel):
    decision: str
    modified_action: str | None = None
    reason: str | None = None

    @field_validator("decision")
    @classmethod
    def _validate_decision(cls, value: str) -> str:
        if value not in ALLOWED_DECISIONS:
            raise ValueError(f"Ungültige Entscheidung. Erlaubt: {', '.join(sorted(ALLOWED_DECISIONS))}")
        return value

    @field_validator("modified_action")
    @classmethod
    def _validate_modified_action(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Angepasste Aktion", max_length=280)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Grund", max_length=280)


class OutcomeInput(BaseModel):
    outcome_status: str
    outcome_source: str = "user_reported"
    result_notes: str | None = None

    @field_validator("outcome_status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_OUTCOME_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_OUTCOME_STATUSES))}")
        return value

    @field_validator("outcome_source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if value not in ALLOWED_OUTCOME_SOURCES:
            raise ValueError(f"Ungültige Quelle. Erlaubt: {', '.join(sorted(ALLOWED_OUTCOME_SOURCES))}")
        return value

    @field_validator("result_notes")
    @classmethod
    def _validate_notes(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Notiz", max_length=500)


class FeedbackInput(BaseModel):
    helpfulness: str
    reason: str | None = None
    comment: str | None = None

    @field_validator("helpfulness")
    @classmethod
    def _validate_helpfulness(cls, value: str) -> str:
        if value not in ALLOWED_HELPFULNESS:
            raise ValueError(f"Ungültiger Wert. Erlaubt: {', '.join(sorted(ALLOWED_HELPFULNESS))}")
        return value

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_FEEDBACK_REASONS:
            raise ValueError(f"Ungültiger Grund. Erlaubt: {', '.join(sorted(ALLOWED_FEEDBACK_REASONS))}")
        return value

    @field_validator("comment")
    @classmethod
    def _validate_comment(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Kommentar", max_length=MAX_FEEDBACK_COMMENT)


def _draft_to_payload(draft: RecommendationDraft, email: str) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "email": email,
        "category": draft.category,
        "title": draft.title,
        "proposed_action": draft.proposed_action,
        "priority": draft.priority,
        "source_type": "rule_based",
        "source_references": [draft.rule_name],
        "goal_id": draft.goal_id,
        "habit_id": draft.habit_id,
        "confidence": draft.confidence,
        "data_quality": draft.data_quality,
        "status": "proposed",
        "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(days=RECOMMENDATION_VALIDITY_DAYS)).isoformat(),
        "explanation": {
            "rule_name": draft.rule_name,
            "data_used": list(draft.data_used),
            "period_days": draft.period_days,
            "data_points": draft.data_points,
            "data_quality": draft.data_quality,
            "expected_benefit": draft.expected_benefit,
        },
    }


def _require_own_recommendation(email: str, recommendation_id: str) -> dict[str, object]:
    try:
        response = (
            supabase.table(RECOMMENDATION_TABLE)
            .select("*")
            .eq("id", recommendation_id)
            .eq("email", email)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Empfehlung konnte nicht geladen werden.") from exc
    if not response.data:
        # 404, not 403 — see core/auth.py: a manipulated/guessed id must not
        # be distinguishable from a non-existent one.
        raise HTTPException(status_code=404, detail="Empfehlung nicht gefunden.")
    return response.data[0]


def _load_habits_with_stats(email: str, today: date) -> list[dict[str, object]]:
    try:
        habits_raw = (
            supabase.table(HABIT_TABLE).select("*").eq("email", email).eq("status", "active").execute().data or []
        )
    except Exception:
        return []
    try:
        habit_entries = (
            supabase.table(HABIT_ENTRY_TABLE)
            .select("habit_id,entry_date,completed")
            .eq("email", email)
            .execute()
            .data
            or []
        )
    except Exception:
        habit_entries = []

    entries_by_habit: dict[str, list[dict[str, object]]] = {}
    for entry in habit_entries:
        entries_by_habit.setdefault(str(entry.get("habit_id")), []).append(entry)

    habits = []
    for habit in habits_raw:
        habit_id = str(habit.get("id"))
        stats = compute_habit_stats(
            entries_by_habit.get(habit_id, []), habit_created_at=habit.get("created_at"), today=today
        )
        habits.append({**habit, **stats})
    return habits


@router.get("")
async def list_recommendations(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        daily_entries = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .order("entry_date", desc=True)
            .limit(30)
            .execute()
            .data
            or []
        )
    except Exception:
        daily_entries = []

    habits = _load_habits_with_stats(email, today)

    try:
        goals = (
            supabase.table(GOAL_TABLE)
            .select("*")
            .eq("email", email)
            .eq("status", "active")
            .is_("deleted_at", "null")
            .execute()
            .data
            or []
        )
    except Exception:
        goals = []

    try:
        history = (
            supabase.table(RECOMMENDATION_TABLE)
            .select("*")
            .eq("email", email)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception:
        history = []

    # Expire anything past valid_until that's still "proposed" (§1 status
    # lifecycle) before deciding what's currently active.
    active_existing = []
    for rec in history:
        valid_until = rec.get("valid_until")
        if rec.get("status") == "proposed" and valid_until and str(valid_until) < now_iso:
            try:
                supabase.table(RECOMMENDATION_TABLE).update({"status": "expired"}).eq("id", rec["id"]).execute()
            except Exception:
                pass
            rec = {**rec, "status": "expired"}
        if rec.get("status") in ACTIVE_STATUSES:
            active_existing.append(rec)

    penalties = personalization.compute_category_penalty(history)
    drafts = generate_recommendations(daily_entries=daily_entries, habits=habits, goals=goals, today=today)

    created: list[dict[str, object]] = []
    for draft in drafts:
        if personalization.should_deprioritize_category(draft.category, penalties):
            continue
        if personalization.has_recent_unsuccessful_duplicate(draft.category, draft.proposed_action, history, today=today):
            continue
        already_active = any(
            rec.get("category") == draft.category
            and rec.get("proposed_action") == draft.proposed_action
            and rec.get("status") in ACTIVE_STATUSES
            for rec in active_existing
        )
        if already_active:
            continue

        payload = _draft_to_payload(draft, email)
        try:
            response = supabase.table(RECOMMENDATION_TABLE).insert(payload).execute()
            if response.data:
                created.append(response.data[0])
        except Exception:
            continue

    if created:
        record_audit_event(
            user_id=None,
            email=email,
            action="create",
            entity_type="recommendation_batch",
            metadata={"count": len(created)},
        )

    return {"items": active_existing + created}


@router.post("/{recommendation_id}/decision")
async def decide_recommendation(
    recommendation_id: str, data: DecisionInput, authorization: str | None = Header(default=None)
):
    email = _require_email(authorization)
    recommendation = _require_own_recommendation(email, recommendation_id)

    if data.decision == "modified" and not data.modified_action:
        raise HTTPException(status_code=422, detail="Bitte gib die angepasste Aktion an.")

    decision_payload = {
        "recommendation_id": recommendation_id,
        "decision": data.decision,
        "original_action": recommendation.get("proposed_action"),
        "modified_action": data.modified_action,
        "reason": data.reason,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table(DECISION_TABLE).insert(decision_payload).execute()
        supabase.table(RECOMMENDATION_TABLE).update(
            {"status": data.decision, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", recommendation_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Entscheidung konnte nicht gespeichert werden.") from exc

    record_audit_event(
        user_id=None,
        email=email,
        action="update",
        entity_type="recommendation",
        entity_id=recommendation_id,
        metadata={"decision": data.decision},
    )
    if data.decision in ("rejected", "skipped"):
        # Etappe 5 §4: dokumentiert als Twin Learning Event (Lernschritt),
        # getrennt vom reinen Compliance-Audit-Log oben.
        record_learning_event(
            user_id=None,
            email=email,
            event_type="empfehlung_abgelehnt",
            source_type="recommendation_decision",
            source_id=recommendation_id,
            previous_state={"status": recommendation.get("status")},
            new_state={"status": data.decision},
            reason=data.reason,
        )
    return {"message": "Entscheidung gespeichert.", "status": data.decision}


@router.post("/{recommendation_id}/outcome")
async def report_outcome(
    recommendation_id: str, data: OutcomeInput, authorization: str | None = Header(default=None)
):
    email = _require_email(authorization)
    _require_own_recommendation(email, recommendation_id)

    payload = {
        "recommendation_id": recommendation_id,
        "outcome_status": data.outcome_status,
        "outcome_source": data.outcome_source,
        "result_notes": data.result_notes,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table(OUTCOME_TABLE).insert(payload).execute()
        update_payload: dict[str, object] = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if data.outcome_status == "completed":
            update_payload["status"] = "completed"
        supabase.table(RECOMMENDATION_TABLE).update(update_payload).eq("id", recommendation_id).eq(
            "email", email
        ).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ergebnis konnte nicht gespeichert werden.") from exc

    record_audit_event(
        user_id=None, email=email, action="update", entity_type="recommendation_outcome", entity_id=recommendation_id
    )
    if data.outcome_status == "completed":
        record_learning_event(
            user_id=None,
            email=email,
            event_type="empfehlung_erfolgreich",
            source_type="recommendation_outcome",
            source_id=recommendation_id,
            new_state={"outcome_status": data.outcome_status},
            reason=data.result_notes,
        )
    return {"message": "Ergebnis gespeichert."}


@router.post("/{recommendation_id}/feedback")
async def submit_feedback(
    recommendation_id: str, data: FeedbackInput, authorization: str | None = Header(default=None)
):
    email = _require_email(authorization)
    _require_own_recommendation(email, recommendation_id)

    payload = {
        "recommendation_id": recommendation_id,
        "helpfulness": data.helpfulness,
        "reason": data.reason,
        "comment": data.comment,
    }
    try:
        supabase.table(FEEDBACK_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Feedback konnte nicht gespeichert werden.") from exc

    record_audit_event(
        user_id=None,
        email=email,
        action="create",
        entity_type="recommendation_feedback",
        entity_id=recommendation_id,
    )
    return {"message": "Danke für dein Feedback."}


@router.get("/{recommendation_id}/why")
async def explain_recommendation(recommendation_id: str, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    recommendation = _require_own_recommendation(email, recommendation_id)
    return build_explanation_response(recommendation)
