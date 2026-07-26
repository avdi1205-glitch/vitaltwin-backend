"""Health data — CGM (Continuous Glucose Monitor) CSV import and manual
nutrition logging.

Mounted at `/api/health` in `app/main.py`. This is the first real,
working ingestion pipeline for individual CGM/nutrition data in this
codebase (every other Founder-OS module explicitly states "keine
individuellen Wellness-/CGM-/Nutrition-Daten" because none existed yet),
so it is scoped and secured deliberately:

- Every endpoint requires a valid session (`core.auth.require_email`) and
  only ever reads/writes rows scoped to the authenticated user's own
  email — there is no endpoint that accepts an arbitrary user identifier,
  and the frontend never sends one.
- The CSV upload is bounded (max file size, max row count) and only
  recognizes two real, documented export formats — FreeStyle LibreView
  (German export) and Dexcom Clarity. An unrecognized file is rejected
  with an honest 400 instead of silently importing garbage rows.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from ..core.auth import require_email
from ..core.supabase import supabase

router = APIRouter()

CGM_TABLE = "vt_cgm_readings"
NUTRITION_TABLE = "vt_nutrition_entries"

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB — a multi-week CGM export is a few hundred KB at most.
MAX_ROWS_PER_UPLOAD = 10_000
INSERT_CHUNK_SIZE = 500

# Known column names per export format (matched case-insensitively).
_TIMESTAMP_COLUMNS: dict[str, list[str]] = {
    "libreview": ["gerätezeitstempel", "geraetezeitstempel", "device timestamp"],
    "dexcom": ["timestamp (yyyy-mm-dd hh:mm:ss)", "timestamp"],
}
_GLUCOSE_COLUMNS: dict[str, list[str]] = {
    "libreview": ["glukosewert-verlauf mg/dl", "glukose-scan mg/dl", "historic glucose mg/dl", "scan glucose mg/dl"],
    "dexcom": ["glucose value (mg/dl)"],
}
_TIMESTAMP_FORMATS = ("%d-%m-%Y %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def _detect_format_and_rows(text: str) -> tuple[str, list[dict[str, str]]]:
    """Finds the header row (some LibreView exports have a summary line
    before it) and returns `(format_name, rows)`. Raises `ValueError` with
    a user-facing message if no recognized format's columns are found."""
    lines = text.splitlines()
    for offset in range(min(5, len(lines))):
        candidate = csv.DictReader(lines[offset:])
        fieldnames = candidate.fieldnames or []
        fields_lower = {(f or "").strip().lower() for f in fieldnames}
        for fmt, timestamp_cols in _TIMESTAMP_COLUMNS.items():
            has_timestamp = any(c in fields_lower for c in timestamp_cols)
            has_glucose = any(c in fields_lower for c in _GLUCOSE_COLUMNS[fmt])
            if has_timestamp and has_glucose:
                reader = csv.DictReader(lines[offset:])
                return fmt, list(reader)
    raise ValueError("Unbekanntes CSV-Format. Unterstützt werden aktuell nur LibreView- und Dexcom-Exporte.")


def _parse_timestamp(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


class NutritionEntryInput(BaseModel):
    meal_name: str = Field(min_length=1, max_length=200)
    carbs: float = Field(ge=0, le=2000)
    protein: float = Field(ge=0, le=2000)
    fat: float = Field(ge=0, le=2000)
    calories: float = Field(ge=0, le=20000)
    timestamp: str | None = None

    @field_validator("meal_name")
    @classmethod
    def _strip_meal_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Mahlzeit darf nicht leer sein.")
        return stripped


@router.post("/cgm/upload-csv")
async def upload_cgm_csv(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    email = require_email(authorization)

    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Datei zu groß (max. 5 MB).")

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Datei konnte nicht gelesen werden (unbekannte Zeichenkodierung).")

    try:
        source, rows = _detect_format_and_rows(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    timestamp_columns = _TIMESTAMP_COLUMNS[source]
    glucose_columns = _GLUCOSE_COLUMNS[source]

    rows_to_insert: list[dict[str, object]] = []
    for row in rows:
        if len(rows_to_insert) >= MAX_ROWS_PER_UPLOAD:
            break
        lower_row = {(k or "").strip().lower(): v for k, v in row.items()}
        raw_timestamp = next((lower_row[c] for c in timestamp_columns if lower_row.get(c)), None)
        raw_glucose = next((lower_row[c] for c in glucose_columns if lower_row.get(c)), None)
        if not raw_timestamp or not raw_glucose:
            continue
        parsed_timestamp = _parse_timestamp(raw_timestamp)
        if not parsed_timestamp:
            continue
        try:
            glucose_value = float(str(raw_glucose).replace(",", "."))
        except ValueError:
            continue
        rows_to_insert.append({
            "email": email,
            "glucose_value": glucose_value,
            "reading_at": parsed_timestamp,
            "source": source,
        })

    if not rows_to_insert:
        raise HTTPException(status_code=400, detail="Keine gültigen Messwerte in der Datei gefunden.")

    try:
        for start in range(0, len(rows_to_insert), INSERT_CHUNK_SIZE):
            chunk = rows_to_insert[start:start + INSERT_CHUNK_SIZE]
            supabase.table(CGM_TABLE).insert(chunk).execute()
    except Exception:
        raise HTTPException(status_code=502, detail="Messwerte konnten nicht gespeichert werden.")

    return {"count": len(rows_to_insert), "source": source}


@router.get("/cgm")
async def list_cgm_readings(days: int = 7, authorization: str | None = Header(default=None)):
    email = require_email(authorization)
    bounded_days = max(1, min(days, 90))
    since = (datetime.now(timezone.utc) - timedelta(days=bounded_days)).isoformat()

    try:
        rows = (
            supabase.table(CGM_TABLE)
            .select("glucose_value,reading_at,source")
            .eq("email", email)
            .gte("reading_at", since)
            .order("reading_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    return [
        {"timestamp": row.get("reading_at"), "glucose_value": row.get("glucose_value"), "source": row.get("source")}
        for row in rows
    ]


@router.post("/nutrition")
async def create_nutrition_entry(data: NutritionEntryInput, authorization: str | None = Header(default=None)):
    email = require_email(authorization)
    logged_at = data.timestamp or datetime.now(timezone.utc).isoformat()

    payload = {
        "email": email,
        "meal_name": data.meal_name,
        "carbs": data.carbs,
        "protein": data.protein,
        "fat": data.fat,
        "calories": data.calories,
        "logged_at": logged_at,
    }
    try:
        supabase.table(NUTRITION_TABLE).insert(payload).execute()
    except Exception:
        raise HTTPException(status_code=502, detail="Mahlzeit konnte nicht gespeichert werden.")

    return {"status": "ok"}


@router.get("/nutrition")
async def list_nutrition_entries(days: int = 7, authorization: str | None = Header(default=None)):
    email = require_email(authorization)
    bounded_days = max(1, min(days, 90))
    since = (datetime.now(timezone.utc) - timedelta(days=bounded_days)).isoformat()

    try:
        rows = (
            supabase.table(NUTRITION_TABLE)
            .select("meal_name,carbs,protein,fat,calories,logged_at")
            .eq("email", email)
            .gte("logged_at", since)
            .order("logged_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    return rows
