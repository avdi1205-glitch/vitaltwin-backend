"""Daily Planning, Evening/Weekly Reflection, Monthly Progress foundation,
and Twin Maturity.

Twin Intelligence Core — Etappe 6.

Endpoints (mounted at `/api/planning` in `app/main.py`):

- `GET  /today`                    today's plan (generated once per local
                                     day, then idempotent — see §2 "Ein
                                     Tagesplan darf am selben Tag aktualisiert
                                     werden. Keine doppelten unkontrollierten
                                     Tagespläne.").
- `PATCH /actions/{id}`            Nutzeranpassung einer Aktion.
- `POST /actions/{id}/decision`    Übernehmen/Ablehnen.
- `POST /actions/{id}/complete`    Aktion erledigen.
- `POST /reflection`               Abendreflexion speichern (ein Eintrag pro
                                     lokalem Tag, wie `/api/profile/daily`).
- `GET  /reflection/today`         heutige Reflexion laden, falls vorhanden.
- `GET  /weekly`                   Wochenrückblick (wird bei jedem Aufruf neu
                                     berechnet und aktualisiert — wie die
                                     Empfehlungsgenerierung in Etappe 4).
- `GET  /monthly`                  Monatsgrundlage (nur Vorschau, keine
                                     eigene Tabelle, siehe
                                     `services/monthly_progress.py`).
- `GET  /maturity`                 Twin-Reifegrad (siehe
                                     `services/twin_maturity.py`).

Nutzertrennung: jeder Endpunkt löst `email` serverseitig aus dem
Session-Token auf; jeder Zugriff auf eine fremde/nicht existierende `id`
liefert 404, nie 403 (siehe `core/auth.py`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.auth import require_email as _require_email_dependency
from ..core.supabase import supabase
from ..core.validation import MAX_REFLECTION_TEXT, validate_scale_1_to_10, validate_short_text
from ..services import daily_planning, monthly_progress, twin_maturity, weekly_reflection
from ..services.habit_service import compute_habit_stats

router = APIRouter()

DAILY_PLAN_TABLE = "vt_daily_plans"
DAILY_PLAN_ACTION_TABLE = "vt_daily_plan_actions"
DAILY_REFLECTION_TABLE = "vt_daily_reflections"
WEEKLY_REFLECTION_TABLE = "vt_weekly_reflections"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
HABIT_TABLE = "vt_habits"
HABIT_ENTRY_TABLE = "vt_habit_entries"
GOAL_TABLE = "vt_wellness_goals"
RECOMMENDATION_TABLE = "vt_recommendations"
MEMORY_TABLE = "vt_twin_memory"
PATTERN_TABLE = "vt_twin_patterns"

ALLOWED_ACTION_DECISIONS = {"accepted", "rejected"}
PREFERRED_TIME_MEMORY_TYPE = "bevorzugte_aktivitaetszeit"
ROUTINE_MEMORY_TYPES = {"erfolgreiche_routine", "bevorzugte_aktivitaetszeit"}


def _require_email(authorization: str | None) -> str:
    return _require_email_dependency(authorization)


class ActionAdjustmentInput(BaseModel):
    description: str

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        cleaned = validate_short_text(value, field_name="Beschreibung", max_length=280)
        if not cleaned:
            raise ValueError("Beschreibung darf nicht leer sein.")
        return cleaned


class ActionDecisionInput(BaseModel):
    decision: str

    @field_validator("decision")
    @classmethod
    def _validate_decision(cls, value: str) -> str:
        if value not in ALLOWED_ACTION_DECISIONS:
            raise ValueError(f"Ungültige Entscheidung. Erlaubt: {', '.join(sorted(ALLOWED_ACTION_DECISIONS))}")
        return value


class ReflectionInput(BaseModel):
    completed_summary: str | None = None
    helpful_note: str | None = None
    difficult_note: str | None = None
    mood: int | None = None
    energy: int | None = None
    tomorrow_change: str | None = None

    @field_validator("completed_summary", "helpful_note", "difficult_note", "tomorrow_change")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Text", max_length=MAX_REFLECTION_TEXT)

    @field_validator("mood")
    @classmethod
    def _validate_mood(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Stimmung")

    @field_validator("energy")
    @classmethod
    def _validate_energy(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Energie")


def _load_habits_with_stats(email: str, today: date) -> list[dict[str, object]]:
    try:
        habits_raw = supabase.table(HABIT_TABLE).select("*").eq("email", email).execute().data or []
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


def _require_own_action(email: str, action_id: str) -> dict[str, object]:
    try:
        response = (
            supabase.table(DAILY_PLAN_ACTION_TABLE).select("*").eq("id", action_id).eq("email", email).limit(1).execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Aktion konnte nicht geladen werden.") from exc
    if not response.data:
        raise HTTPException(status_code=404, detail="Aktion nicht gefunden.")
    return response.data[0]


def _build_memory_candidate_notes(helpful_note: str | None, difficult_note: str | None) -> list[str]:
    """Etappe 6 §3: "mögliche Memory-Kandidaten" — nur ein transparenter
    Hinweis für eine spätere manuelle/automatische Prüfung (Etappe 5), keine
    automatische Erstellung einer echten Memory hier (das würde die
    Etappen-Grenze zwischen Reflection Loop und Memory Loop verwischen)."""
    notes: list[str] = []
    if helpful_note:
        notes.append(f'Möglicher Hinweis auf eine hilfreiche Routine: "{helpful_note}"')
    if difficult_note:
        notes.append(f'Möglicher Hinweis auf eine schwierige Gewohnheit/Empfehlung: "{difficult_note}"')
    return notes


# ---------------------------------------------------------------------------
# Daily Planning Loop (Etappe 6 §1-2)
# ---------------------------------------------------------------------------


@router.get("/today")
async def get_today_plan(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()
    now = datetime.now(timezone.utc)

    try:
        existing_plan = (
            supabase.table(DAILY_PLAN_TABLE)
            .select("*")
            .eq("email", email)
            .eq("local_date", today.isoformat())
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        existing_plan = []

    if existing_plan:
        plan = existing_plan[0]
        try:
            actions = (
                supabase.table(DAILY_PLAN_ACTION_TABLE)
                .select("*")
                .eq("daily_plan_id", plan["id"])
                .order("sort_order")
                .execute()
                .data
                or []
            )
        except Exception:
            actions = []
        return {"plan": plan, "actions": actions}

    # Kein Plan für heute -> neu generieren (einmalig pro Tag, siehe
    # Modul-Docstring).
    goals = []
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
        pass

    habits = _load_habits_with_stats(email, today)

    try:
        recommendations = (
            supabase.table(RECOMMENDATION_TABLE)
            .select("*")
            .eq("email", email)
            .eq("status", "proposed")
            .execute()
            .data
            or []
        )
    except Exception:
        recommendations = []

    yesterday = (today - timedelta(days=1)).isoformat()
    try:
        yesterday_plan = (
            supabase.table(DAILY_PLAN_TABLE).select("id").eq("email", email).eq("local_date", yesterday).limit(1).execute().data
        )
    except Exception:
        yesterday_plan = []
    yesterday_actions: list[dict[str, object]] = []
    if yesterday_plan:
        try:
            yesterday_actions = (
                supabase.table(DAILY_PLAN_ACTION_TABLE)
                .select("*")
                .eq("daily_plan_id", yesterday_plan[0]["id"])
                .execute()
                .data
                or []
            )
        except Exception:
            yesterday_actions = []

    try:
        preferred_time_memories = (
            supabase.table(MEMORY_TABLE)
            .select("normalized_value")
            .eq("email", email)
            .eq("memory_type", PREFERRED_TIME_MEMORY_TYPE)
            .in_("status", ["active", "confirmed"])
            .execute()
            .data
            or []
        )
    except Exception:
        preferred_time_memories = []
    preferred_time_habit_ids = {
        str(m["normalized_value"]["habit_id"])
        for m in preferred_time_memories
        if isinstance(m.get("normalized_value"), dict) and m["normalized_value"].get("habit_id")
    }

    drafts = daily_planning.generate_daily_plan_actions(
        goals=goals,
        habits=habits,
        recommendations=recommendations,
        yesterday_actions=yesterday_actions,
        preferred_time_habit_ids=preferred_time_habit_ids,
        current_hour=now.hour,
    )

    plan_payload = {
        "email": email,
        "local_date": today.isoformat(),
        "timezone": "Europe/Berlin",
        "status": "active",
        "source": "calculated",
    }
    try:
        plan_response = supabase.table(DAILY_PLAN_TABLE).insert(plan_payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Tagesplan konnte nicht erstellt werden.") from exc
    plan = plan_response.data[0] if plan_response.data else plan_payload

    action_rows: list[dict[str, object]] = []
    for sort_order, draft in enumerate(drafts):
        action_payload = {
            "daily_plan_id": plan.get("id"),
            "email": email,
            "description": draft.description,
            "reasoning": draft.reasoning,
            "estimated_effort": draft.estimated_effort,
            "priority": draft.priority,
            "source": draft.source,
            "status": "proposed",
            "sort_order": sort_order,
            "goal_id": draft.goal_id,
            "habit_id": draft.habit_id,
            "recommendation_id": draft.recommendation_id,
            "carried_over": draft.carried_over,
        }
        try:
            action_response = supabase.table(DAILY_PLAN_ACTION_TABLE).insert(action_payload).execute()
        except Exception:
            continue
        if action_response.data:
            action_rows.append(action_response.data[0])

    return {"plan": plan, "actions": action_rows}


@router.patch("/actions/{action_id}")
async def adjust_action(action_id: str, data: ActionAdjustmentInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    action = _require_own_action(email, action_id)
    now = datetime.now(timezone.utc)

    new_status = daily_planning.next_status_after_adjustment(str(action.get("status")))
    updates = {"user_adjusted_description": data.description, "status": new_status, "updated_at": now.isoformat()}
    try:
        supabase.table(DAILY_PLAN_ACTION_TABLE).update(updates).eq("id", action_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Aktion konnte nicht angepasst werden.") from exc
    return {**action, **updates}


@router.post("/actions/{action_id}/decision")
async def decide_action(action_id: str, data: ActionDecisionInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    action = _require_own_action(email, action_id)
    now = datetime.now(timezone.utc)

    updates = {"status": data.decision, "updated_at": now.isoformat()}
    try:
        supabase.table(DAILY_PLAN_ACTION_TABLE).update(updates).eq("id", action_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Entscheidung konnte nicht gespeichert werden.") from exc
    return {**action, **updates}


@router.post("/actions/{action_id}/complete")
async def complete_action(action_id: str, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    action = _require_own_action(email, action_id)
    now = datetime.now(timezone.utc)

    updates = {"status": "completed", "updated_at": now.isoformat()}
    try:
        supabase.table(DAILY_PLAN_ACTION_TABLE).update(updates).eq("id", action_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Aktion konnte nicht als erledigt markiert werden.") from exc
    return {**action, **updates}


# ---------------------------------------------------------------------------
# Evening Reflection Loop (Etappe 6 §3)
# ---------------------------------------------------------------------------


@router.post("/reflection")
async def save_reflection(data: ReflectionInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()
    now = datetime.now(timezone.utc)

    try:
        today_plan = (
            supabase.table(DAILY_PLAN_TABLE)
            .select("id")
            .eq("email", email)
            .eq("local_date", today.isoformat())
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        today_plan = []

    plan_outcome: dict[str, object] = {}
    daily_plan_id = today_plan[0]["id"] if today_plan else None
    if daily_plan_id:
        try:
            actions = (
                supabase.table(DAILY_PLAN_ACTION_TABLE).select("status").eq("daily_plan_id", daily_plan_id).execute().data
                or []
            )
            total = len(actions)
            completed = sum(1 for a in actions if a.get("status") == "completed")
            plan_outcome = {"total_actions": total, "completed_actions": completed}
        except Exception:
            plan_outcome = {}

    payload = data.model_dump(exclude_none=True)
    payload["email"] = email
    payload["local_date"] = today.isoformat()
    payload["daily_plan_id"] = daily_plan_id
    payload["plan_outcome"] = plan_outcome
    payload["memory_candidate_notes"] = _build_memory_candidate_notes(data.helpful_note, data.difficult_note)
    payload["updated_at"] = now.isoformat()

    try:
        existing = (
            supabase.table(DAILY_REFLECTION_TABLE)
            .select("id")
            .eq("email", email)
            .eq("local_date", today.isoformat())
            .limit(1)
            .execute()
        )
        if existing.data:
            supabase.table(DAILY_REFLECTION_TABLE).update(payload).eq("email", email).eq(
                "local_date", today.isoformat()
            ).execute()
        else:
            supabase.table(DAILY_REFLECTION_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Reflexion konnte nicht gespeichert werden.") from exc

    return {"message": "Reflexion gespeichert.", "plan_outcome": plan_outcome}


@router.get("/reflection/today")
async def get_today_reflection(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()
    try:
        response = (
            supabase.table(DAILY_REFLECTION_TABLE)
            .select("*")
            .eq("email", email)
            .eq("local_date", today.isoformat())
            .limit(1)
            .execute()
        )
        return {"item": response.data[0] if response.data else None}
    except Exception:
        return {"item": None}


# ---------------------------------------------------------------------------
# Weekly Reflection Loop (Etappe 6 §4)
# ---------------------------------------------------------------------------


@router.get("/weekly")
async def get_weekly_reflection(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    previous_week_start = week_start - timedelta(days=7)
    now = datetime.now(timezone.utc)

    try:
        all_entries = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .order("entry_date", desc=True)
            .limit(60)
            .execute()
            .data
            or []
        )
    except Exception:
        all_entries = []

    def _in_window(entry: dict[str, object], start: date, end: date) -> bool:
        raw = entry.get("entry_date")
        if not raw:
            return False
        entry_date = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        return start <= entry_date <= end

    this_week_entries = [e for e in all_entries if _in_window(e, week_start, today)]
    previous_week_entries = [e for e in all_entries if _in_window(e, previous_week_start, week_start - timedelta(days=1))]

    habits = _load_habits_with_stats(email, today)

    try:
        goals = supabase.table(GOAL_TABLE).select("*").eq("email", email).is_("deleted_at", "null").execute().data or []
    except Exception:
        goals = []

    try:
        recommendation_history = (
            supabase.table(RECOMMENDATION_TABLE).select("*").eq("email", email).limit(200).execute().data or []
        )
    except Exception:
        recommendation_history = []

    try:
        confirmed_patterns = (
            supabase.table(PATTERN_TABLE)
            .select("*")
            .eq("email", email)
            .eq("status", "active")
            .eq("contradicting", False)
            .execute()
            .data
            or []
        )
    except Exception:
        confirmed_patterns = []

    result = weekly_reflection.compute_weekly_reflection(
        this_week_entries=this_week_entries,
        previous_week_entries=previous_week_entries,
        habits=habits,
        goals=goals,
        recommendation_history=recommendation_history,
        confirmed_patterns=confirmed_patterns,
    )

    payload = {
        "email": email,
        "week_start_date": week_start.isoformat(),
        "data_sufficient": result.data_sufficient,
        "data_points": result.data_points,
        "summary": result.summary,
        "positive_developments": result.positive_developments,
        "stable_routines": result.stable_routines,
        "potential_areas": result.potential_areas,
        "goal_progress": result.goal_progress,
        "most_helpful_recommendations": result.most_helpful_recommendations,
        "least_helpful_recommendations": result.least_helpful_recommendations,
        "suggestions_next_week": result.suggestions_next_week,
        "patterns": result.patterns,
        "updated_at": now.isoformat(),
    }

    try:
        existing = (
            supabase.table(WEEKLY_REFLECTION_TABLE)
            .select("id")
            .eq("email", email)
            .eq("week_start_date", week_start.isoformat())
            .limit(1)
            .execute()
        )
        if existing.data:
            supabase.table(WEEKLY_REFLECTION_TABLE).update(payload).eq("email", email).eq(
                "week_start_date", week_start.isoformat()
            ).execute()
        else:
            supabase.table(WEEKLY_REFLECTION_TABLE).insert(payload).execute()
    except Exception:
        pass

    return payload


# ---------------------------------------------------------------------------
# Monthly Progress — Grundlage (Etappe 6 §5)
# ---------------------------------------------------------------------------


@router.get("/monthly")
async def get_monthly_progress(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()

    try:
        daily_entries = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .order("entry_date", desc=True)
            .limit(60)
            .execute()
            .data
            or []
        )
    except Exception:
        daily_entries = []

    habits = _load_habits_with_stats(email, today)

    try:
        goals = supabase.table(GOAL_TABLE).select("*").eq("email", email).is_("deleted_at", "null").execute().data or []
    except Exception:
        goals = []

    try:
        confirmed_memories = (
            supabase.table(MEMORY_TABLE)
            .select("*")
            .eq("email", email)
            .in_("status", ["active", "confirmed"])
            .execute()
            .data
            or []
        )
    except Exception:
        confirmed_memories = []

    try:
        confirmed_patterns = (
            supabase.table(PATTERN_TABLE).select("*").eq("email", email).eq("status", "active").execute().data or []
        )
    except Exception:
        confirmed_patterns = []

    result = monthly_progress.prepare_monthly_progress(
        daily_entries=daily_entries,
        habits=habits,
        goals=goals,
        confirmed_memories=confirmed_memories,
        confirmed_patterns=confirmed_patterns,
        today=today,
    )
    return {
        "available": result.available,
        "data_points": result.data_points,
        "reason": result.reason,
        "thirty_day_trends": result.thirty_day_trends,
        "goal_development": result.goal_development,
        "habit_summary": result.habit_summary,
        "changed_preferences": result.changed_preferences,
        "confirmed_patterns": result.confirmed_patterns,
        "memory_development": result.memory_development,
        "next_month_goal_suggestions": result.next_month_goal_suggestions,
    }


# ---------------------------------------------------------------------------
# Twin-Reifegrad (Etappe 6 §6)
# ---------------------------------------------------------------------------


@router.get("/maturity")
async def get_twin_maturity(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()

    try:
        entries = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("entry_date")
            .eq("email", email)
            .order("entry_date")
            .execute()
            .data
            or []
        )
    except Exception:
        entries = []
    distinct_days = {e.get("entry_date") for e in entries if e.get("entry_date")}
    checkin_day_count = len(distinct_days)
    account_age_days = 0
    if distinct_days:
        earliest = min(date.fromisoformat(str(d)) for d in distinct_days)
        account_age_days = max(0, (today - earliest).days)

    try:
        memories = (
            supabase.table(MEMORY_TABLE)
            .select("memory_type,status")
            .eq("email", email)
            .in_("status", ["active", "confirmed"])
            .execute()
            .data
            or []
        )
    except Exception:
        memories = []
    confirmed_memory_count = sum(1 for m in memories if m.get("status") == "confirmed")
    has_routine_or_time_memory = any(m.get("memory_type") in ROUTINE_MEMORY_TYPES for m in memories)
    has_confirmed_preference = any(
        m.get("memory_type") == "bestaetigte_praeferenz" and m.get("status") == "confirmed" for m in memories
    )

    try:
        patterns = (
            supabase.table(PATTERN_TABLE)
            .select("id")
            .eq("email", email)
            .eq("status", "active")
            .eq("contradicting", False)
            .execute()
            .data
            or []
        )
    except Exception:
        patterns = []
    has_active_pattern = len(patterns) > 0

    try:
        weekly_rows = (
            supabase.table(WEEKLY_REFLECTION_TABLE)
            .select("id")
            .eq("email", email)
            .eq("data_sufficient", True)
            .execute()
            .data
            or []
        )
    except Exception:
        weekly_rows = []
    weekly_reflection_count = len(weekly_rows)

    result = twin_maturity.compute_twin_maturity(
        checkin_day_count=checkin_day_count,
        account_age_days=account_age_days,
        confirmed_memory_count=confirmed_memory_count,
        has_routine_or_time_memory=has_routine_or_time_memory,
        has_confirmed_preference=has_confirmed_preference,
        has_active_pattern=has_active_pattern,
        weekly_reflection_count=weekly_reflection_count,
    )
    return {
        "level": result.level,
        "level_label": result.level_label,
        "present_data": result.present_data,
        "missing_data": result.missing_data,
    }
