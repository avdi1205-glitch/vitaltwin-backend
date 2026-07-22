from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..core.supabase import supabase
from ..core.auth import require_email as _require_email_dependency

router = APIRouter()

PROFILE_TABLE = "vt_user_profiles"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
HABIT_TABLE = "vt_habits"
HABIT_ENTRY_TABLE = "vt_habit_entries"

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
    steps: int | None = Field(default=None, ge=0, le=100000)
    stress_level: int | None = Field(default=None, ge=1, le=5)
    energy_level: int | None = Field(default=None, ge=1, le=5)
    nutrition_habit: Literal["meist_unverarbeitet", "gemischt", "meist_verarbeitet"] | None = None
    water_habit: Literal["wenig", "mittel", "viel"] | None = None


class HabitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str
    frequency: str
    target: str | None = Field(default=None, max_length=200)
    reminder_enabled: bool = False
    reminder_time: str | None = None
    active: bool = True

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

    return {
        "message": "Deine Löschanfrage wurde gespeichert. Wir melden uns per E-Mail und bearbeiten sie manuell, "
        "um versehentlichen Datenverlust auszuschließen. Du kannst uns auch direkt unter info@vitaltwin.de erreichen.",
    }


@router.get("/export")
async def export_profile(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)

    profile = _get_profile_row(email) or {}
    try:
        daily = supabase.table(DAILY_ENTRY_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        daily = []
    try:
        habits = supabase.table(HABIT_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        habits = []
    try:
        habit_entries = supabase.table(HABIT_ENTRY_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        habit_entries = []

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "email": email,
        "profile": profile,
        "daily_wellness_entries": daily,
        "habits": habits,
        "habit_entries": habit_entries,
    }


@router.put("/daily")
async def upsert_daily_entry(data: DailyWellnessEntryInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    entry_date = (data.entry_date or date.today()).isoformat()

    payload = data.model_dump(exclude_none=True, exclude={"entry_date"})
    payload["email"] = email
    payload["entry_date"] = entry_date

    try:
        existing = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("id")
            .eq("email", email)
            .eq("entry_date", entry_date)
            .limit(1)
            .execute()
        )
        if existing.data:
            supabase.table(DAILY_ENTRY_TABLE).update(payload).eq("email", email).eq("entry_date", entry_date).execute()
        else:
            supabase.table(DAILY_ENTRY_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Tageswert konnte nicht gespeichert werden.") from exc

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


@router.get("/habits")
async def list_habits(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    try:
        response = supabase.table(HABIT_TABLE).select("*").eq("email", email).order("created_at").execute()
        return {"items": response.data or []}
    except Exception:
        return {"items": []}


@router.post("/habits")
async def create_habit(data: HabitCreate, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    payload = data.model_dump()
    payload["email"] = email

    try:
        response = supabase.table(HABIT_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Gewohnheit konnte nicht gespeichert werden.") from exc

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
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        supabase.table(HABIT_TABLE).update(payload).eq("id", habit_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Gewohnheit konnte nicht aktualisiert werden.") from exc

    return _require_own_habit(email, habit_id)


@router.delete("/habits/{habit_id}")
async def delete_habit(habit_id: str, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    _require_own_habit(email, habit_id)

    try:
        supabase.table(HABIT_TABLE).delete().eq("id", habit_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Gewohnheit konnte nicht gelöscht werden.") from exc

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
