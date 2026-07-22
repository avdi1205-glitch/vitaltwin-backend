from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..core.supabase import supabase
from ..core.auth import require_email as _require_email_dependency
from ..core.audit import record_audit_event
from ..core.learning_events import record_learning_event
from ..core.validation import (
    MAX_SYNC_EXPORT_ROWS,
    validate_local_date_not_future,
    validate_movement_minutes,
    validate_scale_1_to_10,
    validate_short_text,
    validate_sleep_hours,
)
from ..services.habit_service import compute_habit_stats
from ..services.privacy_export import count_total_export_rows, exceeds_sync_export_limit
from ..services.trends import compute_trend

router = APIRouter()

PROFILE_TABLE = "vt_user_profiles"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
HABIT_TABLE = "vt_habits"
HABIT_ENTRY_TABLE = "vt_habit_entries"
GOAL_TABLE = "vt_wellness_goals"
DAILY_PLAN_TABLE = "vt_daily_plans"
DAILY_PLAN_ACTION_TABLE = "vt_daily_plan_actions"
DAILY_REFLECTION_TABLE = "vt_daily_reflections"
WEEKLY_REFLECTION_TABLE = "vt_weekly_reflections"
RECOMMENDATION_TABLE = "vt_recommendations"
RECOMMENDATION_DECISION_TABLE = "vt_recommendation_decisions"
RECOMMENDATION_OUTCOME_TABLE = "vt_recommendation_outcomes"
RECOMMENDATION_FEEDBACK_TABLE = "vt_recommendation_feedback"
MEMORY_TABLE = "vt_twin_memory"
PATTERN_TABLE = "vt_twin_patterns"
LEARNING_EVENT_TABLE = "vt_twin_learning_events"
CONSENT_TABLE = "vt_consent_records"

_CURRENT_YEAR = datetime.now(timezone.utc).year

ALLOWED_WELLNESS_GOALS = {
    "besser_schlafen",
    "mehr_bewegen",
    "stress_reduzieren",
    "gesuender_essen",
    "gewicht_bewusst_verwalten",
    "mehr_energie",
    "bessere_erholung",
    "gesunde_gewohnheiten_aufbauen",
}

ALLOWED_HABIT_CATEGORIES = {"schlaf", "bewegung", "ernaehrung", "stress", "energie", "erholung", "sonstiges"}
ALLOWED_HABIT_FREQUENCIES = {"taeglich", "mehrmals_woche", "woechentlich"}
ALLOWED_HABIT_STATUSES = {"active", "paused", "archived"}
ALLOWED_GOAL_CATEGORIES = ALLOWED_WELLNESS_GOALS | {"eigenes_ziel"}
ALLOWED_GOAL_STATUSES = {"active", "paused", "completed", "archived"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _require_email(authorization: str | None) -> str:
    # Delegates to the centralized auth helper (Twin Intelligence Core,
    # Etappe 2) instead of duplicating the "parse Authorization header ->
    # decode JWT" logic here. Kept as a thin wrapper so every existing call
    # site in this file (`_require_email(authorization)`) keeps working
    # unchanged.
    return _require_email_dependency(authorization)


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    birth_year: int | None = Field(default=None, ge=1920, le=_CURRENT_YEAR - 13)
    age_group: Literal["unter_18", "18-24", "25-34", "35-44", "45-54", "55-64", "65_plus"] | None = None
    gender: Literal["weiblich", "maennlich", "divers", "keine_angabe"] | None = None
    height_cm: float | None = Field(default=None, ge=100, le=250)
    weight_kg: float | None = Field(default=None, ge=30, le=300)
    preferred_language: Literal["de", "en"] = "de"
    timezone: str = Field(default="Europe/Berlin", max_length=60)
    unit_system: Literal["metric", "imperial"] = "metric"
    wellness_goals: list[str] = Field(default_factory=list)
    onboarding_completed: bool | None = None

    @field_validator("wellness_goals")
    @classmethod
    def _validate_goals(cls, value: list[str]) -> list[str]:
        invalid = [goal for goal in value if goal not in ALLOWED_WELLNESS_GOALS]
        if invalid:
            raise ValueError(f"Ungültige Wellness-Ziele: {', '.join(invalid)}")
        # Dedupe while preserving order.
        seen: set[str] = set()
        deduped = []
        for goal in value:
            if goal not in seen:
                seen.add(goal)
                deduped.append(goal)
        return deduped

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class DailyWellnessEntryInput(BaseModel):
    entry_date: date | None = None
    sleep_hours: float | None = Field(default=None, ge=0, le=16)
    movement_days_per_week: int | None = Field(default=None, ge=0, le=7)
    movement_minutes: int | None = None
    steps: int | None = Field(default=None, ge=0, le=100000)
    stress_level: int | None = Field(default=None, ge=1, le=5)
    energy_level: int | None = Field(default=None, ge=1, le=5)
    nutrition_habit: Literal["meist_unverarbeitet", "gemischt", "meist_verarbeitet"] | None = None
    water_habit: Literal["wenig", "mittel", "viel"] | None = None
    # Etappe 3: real 1-10 self-report scales (Constitution: energy, mood,
    # stress, motivation, sleep quality, recovery). `stress_level`/
    # `energy_level` above stay on their original 1-5 scale for backward
    # compatibility with the existing onboarding/dashboard forms — these are
    # additive, not a replacement.
    energy: int | None = None
    stress: int | None = None
    mood: int | None = None
    motivation: int | None = None
    sleep_quality: int | None = None
    recovery: int | None = None
    note: str | None = None

    @field_validator("entry_date")
    @classmethod
    def _validate_entry_date(cls, value: date | None) -> date | None:
        return validate_local_date_not_future(value, field_name="Check-in-Datum")

    @field_validator("sleep_hours")
    @classmethod
    def _validate_sleep_hours_field(cls, value: float | None) -> float | None:
        return validate_sleep_hours(value)

    @field_validator("movement_minutes")
    @classmethod
    def _validate_movement_minutes_field(cls, value: int | None) -> int | None:
        return validate_movement_minutes(value)

    @field_validator("mood")
    @classmethod
    def _validate_mood(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Stimmung")

    @field_validator("energy")
    @classmethod
    def _validate_energy(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Energie")

    @field_validator("stress")
    @classmethod
    def _validate_stress(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Stress")

    @field_validator("motivation")
    @classmethod
    def _validate_motivation(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Motivation")

    @field_validator("sleep_quality")
    @classmethod
    def _validate_sleep_quality(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Schlafqualität")

    @field_validator("recovery")
    @classmethod
    def _validate_recovery(cls, value: int | None) -> int | None:
        return validate_scale_1_to_10(value, field_name="Erholung")

    @field_validator("note")
    @classmethod
    def _validate_note(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Notiz", max_length=280)


class HabitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str
    frequency: str
    target: str | None = Field(default=None, max_length=200)
    reminder_enabled: bool = False
    reminder_time: str | None = None
    active: bool = True
    status: str = "active"

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_HABIT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_HABIT_STATUSES))}")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Bitte gib einen Namen für die Gewohnheit ein.")
        return stripped

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        if value not in ALLOWED_HABIT_CATEGORIES:
            raise ValueError(f"Ungültige Kategorie. Erlaubt: {', '.join(sorted(ALLOWED_HABIT_CATEGORIES))}")
        return value

    @field_validator("frequency")
    @classmethod
    def _validate_frequency(cls, value: str) -> str:
        if value not in ALLOWED_HABIT_FREQUENCIES:
            raise ValueError(f"Ungültige Häufigkeit. Erlaubt: {', '.join(sorted(ALLOWED_HABIT_FREQUENCIES))}")
        return value

    @field_validator("reminder_time")
    @classmethod
    def _validate_reminder_time(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _TIME_RE.match(value):
            raise ValueError("Erinnerungszeit muss im Format HH:MM sein (z. B. 07:30).")
        return value


class HabitUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    category: str | None = None
    frequency: str | None = None
    target: str | None = Field(default=None, max_length=200)
    reminder_enabled: bool | None = None
    reminder_time: str | None = None
    active: bool | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def _validate_status_update(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_HABIT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_HABIT_STATUSES))}")
        return value

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_HABIT_CATEGORIES:
            raise ValueError(f"Ungültige Kategorie. Erlaubt: {', '.join(sorted(ALLOWED_HABIT_CATEGORIES))}")
        return value

    @field_validator("frequency")
    @classmethod
    def _validate_frequency(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_HABIT_FREQUENCIES:
            raise ValueError(f"Ungültige Häufigkeit. Erlaubt: {', '.join(sorted(ALLOWED_HABIT_FREQUENCIES))}")
        return value

    @field_validator("reminder_time")
    @classmethod
    def _validate_reminder_time(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _TIME_RE.match(value):
            raise ValueError("Erinnerungszeit muss im Format HH:MM sein (z. B. 07:30).")
        return value


class HabitEntryInput(BaseModel):
    entry_date: date | None = None
    completed: bool = True

    @field_validator("entry_date")
    @classmethod
    def _validate_entry_date(cls, value: date | None) -> date | None:
        return validate_local_date_not_future(value, field_name="Eintragsdatum")


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    goal_type: str
    target_value: float | None = None
    target_date: date | None = None
    status: str = "active"

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Bitte gib einen Titel für das Ziel ein.")
        return stripped

    @field_validator("goal_type")
    @classmethod
    def _validate_goal_type(cls, value: str) -> str:
        if value not in ALLOWED_GOAL_CATEGORIES:
            raise ValueError(f"Ungültige Zielkategorie. Erlaubt: {', '.join(sorted(ALLOWED_GOAL_CATEGORIES))}")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_GOAL_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_GOAL_STATUSES))}")
        return value

    @field_validator("target_date")
    @classmethod
    def _validate_target_date(cls, value: date | None) -> date | None:
        # Target dates are explicitly allowed in the future (that's the
        # point of a goal) — only reject dates before today.
        if value is not None and value < date.today():
            raise ValueError("Zieldatum darf nicht in der Vergangenheit liegen.")
        return value


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    goal_type: str | None = None
    target_value: float | None = None
    target_date: date | None = None
    status: str | None = None

    @field_validator("goal_type")
    @classmethod
    def _validate_goal_type(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_GOAL_CATEGORIES:
            raise ValueError(f"Ungültige Zielkategorie. Erlaubt: {', '.join(sorted(ALLOWED_GOAL_CATEGORIES))}")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status_update(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_GOAL_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_GOAL_STATUSES))}")
        return value


def _get_profile_row(email: str) -> dict[str, object] | None:
    try:
        response = supabase.table(PROFILE_TABLE).select("*").eq("email", email).limit(1).execute()
        return response.data[0] if response.data else None
    except Exception:
        return None


@router.get("/me")
async def get_profile(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    profile = _get_profile_row(email)

    if not profile:
        return {
            "email": email,
            "display_name": None,
            "birth_year": None,
            "age_group": None,
            "gender": None,
            "height_cm": None,
            "weight_kg": None,
            "preferred_language": "de",
            "timezone": "Europe/Berlin",
            "unit_system": "metric",
            "wellness_goals": [],
            "onboarding_completed": False,
            "updated_at": None,
            "deletion_requested_at": None,
        }

    return profile


@router.put("/me")
async def update_profile(data: ProfileUpdate, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)

    payload = data.model_dump(exclude_none=True)
    payload["email"] = email
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        existing = _get_profile_row(email)
        if existing:
            supabase.table(PROFILE_TABLE).update(payload).eq("email", email).execute()
        else:
            supabase.table(PROFILE_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Profil konnte nicht gespeichert werden.") from exc

    return _get_profile_row(email) or payload


@router.post("/request-deletion")
async def request_deletion(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        existing = _get_profile_row(email)
        if existing:
            supabase.table(PROFILE_TABLE).update({"deletion_requested_at": now_iso}).eq("email", email).execute()
        else:
            supabase.table(PROFILE_TABLE).insert({"email": email, "deletion_requested_at": now_iso}).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Löschanfrage konnte nicht gespeichert werden.") from exc

    record_audit_event(user_id=None, email=email, action="deletion_request", entity_type="account")

    return {
        "message": "Deine Löschanfrage wurde gespeichert. Wir melden uns per E-Mail und bearbeiten sie manuell, "
        "um versehentlichen Datenverlust auszuschließen. Du kannst uns auch direkt unter info@vitaltwin.de erreichen.",
    }


@router.get("/export")
async def export_profile(authorization: str | None = Header(default=None)):
    """Etappe 9 §1: vollständiger Datenexport — jede in der Constitution/
    den Etappen 2-7 gespeicherte Kategorie, ausschließlich für den
    anfragenden Nutzer (jede Abfrage `.eq("email", email)`), niemals Daten
    anderer Nutzer. Oberhalb von `MAX_SYNC_EXPORT_ROWS` wird der Export
    abgelehnt statt eine sehr große Antwort zu erzwingen — siehe
    `services/privacy_export.py` und `docs/TWIN_BETA_LIMITATIONS.md`
    ("für spätere Background Jobs vorbereiten")."""
    email = _require_email(authorization)

    def _load(table: str) -> list[dict[str, object]]:
        try:
            return supabase.table(table).select("*").eq("email", email).execute().data or []
        except Exception:
            return []

    profile = _get_profile_row(email) or {}
    bundle = {
        "profile": profile,
        "daily_wellness_entries": _load(DAILY_ENTRY_TABLE),
        "habits": _load(HABIT_TABLE),
        "habit_entries": _load(HABIT_ENTRY_TABLE),
        "goals": _load(GOAL_TABLE),
        "daily_plans": _load(DAILY_PLAN_TABLE),
        "daily_plan_actions": _load(DAILY_PLAN_ACTION_TABLE),
        "daily_reflections": _load(DAILY_REFLECTION_TABLE),
        "weekly_reflections": _load(WEEKLY_REFLECTION_TABLE),
        "recommendations": _load(RECOMMENDATION_TABLE),
        "recommendation_decisions": _load(RECOMMENDATION_DECISION_TABLE),
        "recommendation_outcomes": _load(RECOMMENDATION_OUTCOME_TABLE),
        "recommendation_feedback": _load(RECOMMENDATION_FEEDBACK_TABLE),
        "twin_memories": _load(MEMORY_TABLE),
        "twin_patterns": _load(PATTERN_TABLE),
        "twin_learning_events": _load(LEARNING_EVENT_TABLE),
        "consents": _load(CONSENT_TABLE),
    }

    total_rows = count_total_export_rows(bundle)
    if exceeds_sync_export_limit(total_rows):
        raise HTTPException(
            status_code=413,
            detail=(
                f"Dein Datenumfang ({total_rows} Einträge) übersteigt die Grenze für einen direkten Export "
                f"({MAX_SYNC_EXPORT_ROWS}). Bitte kontaktiere info@vitaltwin.de für einen manuellen Export."
            ),
        )

    record_audit_event(user_id=None, email=email, action="export_request", entity_type="full_export")

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "email": email,
        **bundle,
    }


@router.put("/daily")
async def upsert_daily_entry(data: DailyWellnessEntryInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    entry_date = (data.entry_date or date.today()).isoformat()

    payload = data.model_dump(exclude_none=True, exclude={"entry_date"})
    payload["email"] = email
    payload["entry_date"] = entry_date
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        existing = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("id")
            .eq("email", email)
            .eq("entry_date", entry_date)
            .limit(1)
            .execute()
        )
        is_update = bool(existing.data)
        if is_update:
            supabase.table(DAILY_ENTRY_TABLE).update(payload).eq("email", email).eq("entry_date", entry_date).execute()
        else:
            supabase.table(DAILY_ENTRY_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Tageswert konnte nicht gespeichert werden.") from exc

    # Etappe 3 §1: "Twin-Kontext als veraltet markieren" — a lightweight
    # audit event marks that today's inputs changed, so a later
    # TwinContextService (Etappe 7) knows to rebuild its context instead of
    # serving a stale one. No heavy recomputation happens here yet.
    record_audit_event(
        user_id=None,
        email=email,
        action="update" if is_update else "create",
        entity_type="daily_wellness_entry",
        entity_id=entry_date,
    )

    return {"message": "Gespeichert.", "entry_date": entry_date}


@router.get("/daily")
async def list_daily_entries(days: int = 14, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    days = max(1, min(days, 90))

    try:
        response = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .order("entry_date", desc=True)
            .limit(days)
            .execute()
        )
        return {"items": response.data or []}
    except Exception:
        return {"items": []}


@router.get("/daily/today")
async def get_today_entry(authorization: str | None = Header(default=None)):
    """Today's check-in in the caller's local calendar day. The client is
    responsible for sending its own local date as `entry_date` on `/daily`
    PUT — this endpoint simply looks up whatever was stored for the most
    recent entry_date, since the server has no reliable way to know the
    user's "today" without it (see Etappe 3 §9, Zeitzone)."""
    email = _require_email(authorization)
    today = date.today().isoformat()
    try:
        response = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .eq("entry_date", today)
            .limit(1)
            .execute()
        )
        return {"item": response.data[0] if response.data else None, "entry_date": today}
    except Exception:
        return {"item": None, "entry_date": today}


@router.delete("/daily/{entry_date}")
async def delete_daily_entry(entry_date: str, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    try:
        supabase.table(DAILY_ENTRY_TABLE).delete().eq("email", email).eq("entry_date", entry_date).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Check-in konnte nicht gelöscht werden.") from exc

    record_audit_event(user_id=None, email=email, action="delete", entity_type="daily_wellness_entry", entity_id=entry_date)
    return {"message": "Check-in gelöscht."}


@router.get("/trends")
async def get_trends(authorization: str | None = Header(default=None)):
    """Sleep, movement, stress, and recovery trends over 7 and 30 days
    (Etappe 3 §2-4). Deliberately just transparent averages — no AI
    interpretation, no diagnosis (see `services/trends.py` docstring)."""
    email = _require_email(authorization)

    try:
        response = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .order("entry_date", desc=True)
            .limit(30)
            .execute()
        )
        entries = response.data or []
    except Exception:
        entries = []

    today = date.today()
    fields = ["sleep_hours", "sleep_quality", "movement_minutes", "stress", "recovery", "mood", "energy"]
    trends: dict[str, dict[str, object]] = {}
    for field in fields:
        for window in (7, 30):
            trend = compute_trend(entries, field=field, window_days=window, today=today)
            trends.setdefault(field, {})[f"{window}d"] = {
                "average": trend.average,
                "data_points": trend.data_points,
                "data_quality": trend.data_quality,
            }

    return {"trends": trends, "disclaimer": (
        "Diese Trends sind transparente Durchschnittswerte deiner eigenen Eintragungen \u2014 "
        "keine medizinische Bewertung und keine Diagnose."
    )}


@router.get("/habits")
async def list_habits(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    try:
        habits = supabase.table(HABIT_TABLE).select("*").eq("email", email).order("created_at").execute().data or []
    except Exception:
        return {"items": []}

    try:
        all_entries = (
            supabase.table(HABIT_ENTRY_TABLE)
            .select("habit_id,entry_date,completed")
            .eq("email", email)
            .execute()
            .data
            or []
        )
    except Exception:
        all_entries = []

    entries_by_habit: dict[str, list[dict[str, object]]] = {}
    for entry in all_entries:
        entries_by_habit.setdefault(str(entry.get("habit_id")), []).append(entry)

    today = date.today()
    items = []
    for habit in habits:
        habit_id = str(habit.get("id"))
        stats = compute_habit_stats(
            entries_by_habit.get(habit_id, []),
            habit_created_at=habit.get("created_at"),
            today=today,
        )
        items.append({**habit, **stats})

    return {"items": items}


@router.post("/habits")
async def create_habit(data: HabitCreate, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    payload = data.model_dump()
    # Keep the legacy `active` boolean in sync with the new tri-state
    # `status` (Etappe 3 §5: active/paused/archived) so existing code paths
    # that still filter on `active` continue to work unchanged.
    payload["active"] = payload["status"] == "active"
    payload["email"] = email

    try:
        response = supabase.table(HABIT_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Gewohnheit konnte nicht gespeichert werden.") from exc

    record_audit_event(user_id=None, email=email, action="create", entity_type="habit", entity_id=payload.get("name"))
    return response.data[0] if response.data else payload


def _require_own_habit(email: str, habit_id: str) -> dict[str, object]:
    try:
        response = supabase.table(HABIT_TABLE).select("*").eq("id", habit_id).eq("email", email).limit(1).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Gewohnheit konnte nicht geladen werden.") from exc
    if not response.data:
        raise HTTPException(status_code=404, detail="Gewohnheit nicht gefunden.")
    return response.data[0]


@router.patch("/habits/{habit_id}")
async def update_habit(habit_id: str, data: HabitUpdate, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    _require_own_habit(email, habit_id)

    payload = data.model_dump(exclude_none=True)
    if not payload:
        return _require_own_habit(email, habit_id)
    if "status" in payload:
        payload["active"] = payload["status"] == "active"
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        supabase.table(HABIT_TABLE).update(payload).eq("id", habit_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Gewohnheit konnte nicht aktualisiert werden.") from exc

    record_audit_event(user_id=None, email=email, action="update", entity_type="habit", entity_id=habit_id)
    return _require_own_habit(email, habit_id)


@router.delete("/habits/{habit_id}")
async def delete_habit(habit_id: str, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    _require_own_habit(email, habit_id)

    try:
        supabase.table(HABIT_TABLE).delete().eq("id", habit_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Gewohnheit konnte nicht gelöscht werden.") from exc

    record_audit_event(user_id=None, email=email, action="delete", entity_type="habit", entity_id=habit_id)
    return {"message": "Gewohnheit gelöscht."}


@router.post("/habits/{habit_id}/entries")
async def toggle_habit_entry(habit_id: str, data: HabitEntryInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    _require_own_habit(email, habit_id)
    entry_date = (data.entry_date or date.today()).isoformat()

    try:
        existing = (
            supabase.table(HABIT_ENTRY_TABLE)
            .select("id")
            .eq("habit_id", habit_id)
            .eq("entry_date", entry_date)
            .limit(1)
            .execute()
        )
        if existing.data:
            supabase.table(HABIT_ENTRY_TABLE).update({"completed": data.completed}).eq(
                "habit_id", habit_id
            ).eq("entry_date", entry_date).execute()
        else:
            supabase.table(HABIT_ENTRY_TABLE).insert(
                {
                    "habit_id": habit_id,
                    "email": email,
                    "entry_date": entry_date,
                    "completed": data.completed,
                }
            ).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Eintrag konnte nicht gespeichert werden.") from exc

    return {"message": "Gespeichert.", "entry_date": entry_date, "completed": data.completed}


@router.get("/habits/entries")
async def list_habit_entries(days: int = 30, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    days = max(1, min(days, 180))

    try:
        response = (
            supabase.table(HABIT_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .order("entry_date", desc=True)
            .limit(days * 10)
            .execute()
        )
        return {"items": response.data or []}
    except Exception:
        return {"items": []}


# ---------------------------------------------------------------------------
# Goal Loop (Etappe 3 §6) — vt_wellness_goals, created in Etappe 2.
# ---------------------------------------------------------------------------


@router.get("/goals")
async def list_goals(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    try:
        response = (
            supabase.table(GOAL_TABLE)
            .select("*")
            .eq("email", email)
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .execute()
        )
        return {"items": response.data or []}
    except Exception:
        return {"items": []}


@router.post("/goals")
async def create_goal(data: GoalCreate, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    payload = {
        "email": email,
        "title": data.title,
        "goal_type": data.goal_type,
        "status": data.status,
        "target_value": data.target_value,
        "target_date": data.target_date.isoformat() if data.target_date else None,
        "source": "manual",
    }

    try:
        response = supabase.table(GOAL_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ziel konnte nicht gespeichert werden.") from exc

    record_audit_event(user_id=None, email=email, action="create", entity_type="wellness_goal", entity_id=data.goal_type)
    return response.data[0] if response.data else payload


def _require_own_goal(email: str, goal_id: str) -> dict[str, object]:
    try:
        response = (
            supabase.table(GOAL_TABLE)
            .select("*")
            .eq("id", goal_id)
            .eq("email", email)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ziel konnte nicht geladen werden.") from exc
    if not response.data:
        # 404, not 403 — see core/auth.py: a manipulated/guessed id must not
        # be distinguishable from a non-existent one.
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden.")
    return response.data[0]


@router.patch("/goals/{goal_id}")
async def update_goal(goal_id: str, data: GoalUpdate, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    existing_goal = _require_own_goal(email, goal_id)

    payload = data.model_dump(exclude_none=True)
    if "target_date" in payload and payload["target_date"] is not None:
        payload["target_date"] = data.target_date.isoformat()
    if not payload:
        return _require_own_goal(email, goal_id)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        supabase.table(GOAL_TABLE).update(payload).eq("id", goal_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ziel konnte nicht aktualisiert werden.") from exc

    record_audit_event(user_id=None, email=email, action="update", entity_type="wellness_goal", entity_id=goal_id)
    # Etappe 5 §4: eine inhaltliche Änderung (Status/Zielwert/Zieldatum, nicht
    # nur updated_at) ist ein dokumentierter Twin-Lernschritt — der Twin merkt
    # sich, dass sich das Ziel des Nutzers verändert hat.
    if any(key in payload for key in ("status", "target_value", "target_date", "title")):
        record_learning_event(
            user_id=None,
            email=email,
            event_type="ziel_angepasst",
            source_type="wellness_goal",
            source_id=goal_id,
            previous_state={
                key: existing_goal.get(key) for key in ("status", "target_value", "target_date", "title") if key in payload
            },
            new_state={key: payload[key] for key in ("status", "target_value", "target_date", "title") if key in payload},
            reason=None,
        )
    return _require_own_goal(email, goal_id)


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str, authorization: str | None = Header(default=None)):
    """Soft delete (archives via `deleted_at`), never a hard delete — a
    Goal's history (actions, reflections referencing it) should stay
    queryable. See Etappe 3 §7: "Delete beziehungsweise sichere
    Archivierung"."""
    email = _require_email(authorization)
    _require_own_goal(email, goal_id)

    try:
        supabase.table(GOAL_TABLE).update(
            {"deleted_at": datetime.now(timezone.utc).isoformat(), "status": "archived"}
        ).eq("id", goal_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ziel konnte nicht archiviert werden.") from exc

    record_audit_event(user_id=None, email=email, action="delete", entity_type="wellness_goal", entity_id=goal_id)
    return {"message": "Ziel archiviert."}

