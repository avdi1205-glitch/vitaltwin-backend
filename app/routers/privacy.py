"""Privacy, Consent, Export, and Deletion controls.

Twin Intelligence Core — Etappe 9.

Endpoints (mounted at `/api/privacy` in `app/main.py`):

- `GET  /overview`            Privacy-UI summary (§7): what's stored, what
                               the Twin actively uses, active consents.
- `GET  /consents`            resolved current consent status per purpose.
- `GET  /consents/history`    full append-only consent log.
- `POST /consents`            grant or revoke one purpose (§3).
- `DELETE /data/{category}`   delete an entire data category (§2).
- `GET  /export/csv/{category}` CSV export of one structured category (§1).

The full multi-category JSON export lives at the pre-existing
`GET /api/profile/export` (extended in this etappe, not moved — the
frontend already calls that path, see Etappe 8).

Nutzertrennung: every endpoint resolves `email` server-side; every query is
`.eq("email", email)` — never another user's data, never a client-supplied
id used for authorization.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, field_validator

from ..core.audit import record_audit_event
from ..core.auth import require_email as _require_email_dependency
from ..core.supabase import supabase
from ..services.privacy_export import resolve_current_consents, rows_to_csv

router = APIRouter()

CONSENT_TABLE = "vt_consent_records"

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
CHAT_USAGE_TABLE = "vt_chat_usage"
FEEDBACK_TABLE = "vt_user_feedback"

ALL_CONSENT_TYPES: tuple[str, ...] = (
    "wellness_data_processing",
    "ai_features",
    "chat_storage",
    "wearables_future",
    "marketing",
    "affiliate_tracking",
    "research_optional",
)

# category -> (table, whether it has a direct `email` column for deletion)
CATEGORY_TABLES: dict[str, str] = {
    "checkins": DAILY_ENTRY_TABLE,
    "habits": HABIT_TABLE,
    "habit_entries": HABIT_ENTRY_TABLE,
    "goals": GOAL_TABLE,
    "daily_plans": DAILY_PLAN_TABLE,
    "reflections": DAILY_REFLECTION_TABLE,
    "weekly_reflections": WEEKLY_REFLECTION_TABLE,
    "recommendations": RECOMMENDATION_TABLE,
    "memories": MEMORY_TABLE,
    "patterns": PATTERN_TABLE,
    "chat_history": CHAT_USAGE_TABLE,
    "feedback": FEEDBACK_TABLE,
}


def _require_email(authorization: str | None) -> str:
    return _require_email_dependency(authorization)


class ConsentInput(BaseModel):
    consent_type: str
    granted: bool

    @field_validator("consent_type")
    @classmethod
    def _validate_consent_type(cls, value: str) -> str:
        if value not in ALL_CONSENT_TYPES:
            raise ValueError(f"Ungültiger Einwilligungszweck. Erlaubt: {', '.join(ALL_CONSENT_TYPES)}")
        return value


def _load_category_rows(email: str, category: str) -> list[dict[str, object]]:
    table = CATEGORY_TABLES[category]
    try:
        return supabase.table(table).select("*").eq("email", email).execute().data or []
    except Exception:
        return []


@router.get("/overview")
async def privacy_overview(authorization: str | None = Header(default=None)):
    """Etappe 9 §7: verständliche Übersicht, welche Daten gespeichert sind,
    welche der Twin aktiv verwendet, und welche Einwilligungen aktiv sind —
    alles aus bereits vorhandenen, per `email` skopierten Abfragen, keine
    neue Geschäftslogik."""
    email = _require_email(authorization)

    stored_counts: dict[str, int] = {}
    for category in CATEGORY_TABLES:
        stored_counts[category] = len(_load_category_rows(email, category))

    memories = _load_category_rows(email, "memories")
    active_memories = [m for m in memories if m.get("status") in ("active", "confirmed") and not m.get("deleted_at")]

    patterns = _load_category_rows(email, "patterns")
    active_patterns = [p for p in patterns if p.get("status") == "active" and not p.get("contradicting")]

    try:
        consent_rows = supabase.table(CONSENT_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        consent_rows = []
    resolved_consents = resolve_current_consents(consent_rows)
    consents = {
        consent_type: resolved_consents.get(consent_type, {"granted": None, "changed_at": None})
        for consent_type in ALL_CONSENT_TYPES
    }

    return {
        "stored_data_counts": stored_counts,
        "active_memories_count": len(active_memories),
        "active_patterns_count": len(active_patterns),
        "consents": consents,
        "note": (
            "Der Twin verwendet für Empfehlungen und den Chat ausschließlich aktive/bestätigte Memories und "
            "nicht widersprüchliche Muster — siehe docs/TWIN_CONTEXT.md."
        ),
    }


@router.get("/consents")
async def get_consents(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    try:
        rows = supabase.table(CONSENT_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        rows = []
    resolved = resolve_current_consents(rows)
    return {
        "consents": {
            consent_type: resolved.get(consent_type, {"granted": None, "changed_at": None})
            for consent_type in ALL_CONSENT_TYPES
        }
    }


@router.get("/consents/history")
async def get_consent_history(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    try:
        rows = (
            supabase.table(CONSENT_TABLE).select("*").eq("email", email).order("created_at", desc=True).execute().data
            or []
        )
    except Exception:
        rows = []
    return {"items": rows}


@router.post("/consents")
async def set_consent(data: ConsentInput, authorization: str | None = Header(default=None)):
    """Etappe 9 §3: jede Einwilligung ist ein eigener Zweck, kein
    pauschales "Ja zu allem". Jeder Aufruf fügt eine neue Log-Zeile hinzu
    (append-only) — ein Widerruf ist damit jederzeit nachvollziehbar und
    technisch sofort wirksam (siehe
    `services/privacy_export.py::resolve_current_consents`)."""
    email = _require_email(authorization)
    now = datetime.now(timezone.utc)

    payload = {
        "email": email,
        "consent_type": data.consent_type,
        "granted": data.granted,
        "granted_at": now.isoformat() if data.granted else None,
        "revoked_at": None if data.granted else now.isoformat(),
    }
    try:
        supabase.table(CONSENT_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Einwilligung konnte nicht gespeichert werden.") from exc

    record_audit_event(
        user_id=None,
        email=email,
        action="consent_change",
        entity_type="consent",
        entity_id=data.consent_type,
        metadata={"granted": data.granted},
    )
    return {"message": "Einwilligung gespeichert.", "consent_type": data.consent_type, "granted": data.granted}


@router.delete("/data/{category}")
async def delete_data_category(category: str, authorization: str | None = Header(default=None)):
    """Etappe 9 §2: vollständige Löschung einer Datenkategorie. Danach darf
    diese Kategorie nie mehr im Twin-Kontext, in Empfehlungen, Trends oder
    Patterns auftauchen — das ist strukturell garantiert, weil jede
    Kontext-/Trend-/Empfehlungsabfrage die Tabelle selbst erneut liest
    (siehe `services/twin_context.py`) und eine gelöschte Zeile dort nicht
    mehr existiert."""
    if category not in CATEGORY_TABLES:
        raise HTTPException(
            status_code=422, detail=f"Ungültige Kategorie. Erlaubt: {', '.join(sorted(CATEGORY_TABLES))}"
        )
    email = _require_email(authorization)
    table = CATEGORY_TABLES[category]

    try:
        response = supabase.table(table).delete().eq("email", email).execute()
        deleted_count = len(response.data or [])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Kategorie konnte nicht gelöscht werden.") from exc

    record_audit_event(
        user_id=None,
        email=email,
        action="delete",
        entity_type=f"category:{category}",
        metadata={"deleted_count": deleted_count},
    )
    return {"message": f"Kategorie '{category}' gelöscht.", "deleted_count": deleted_count}


@router.get("/export/csv/{category}")
async def export_category_csv(category: str, authorization: str | None = Header(default=None)):
    """Etappe 9 §1: optionales CSV für strukturierte Daten — ergänzend zum
    vollständigen JSON-Export unter `GET /api/profile/export`."""
    if category not in CATEGORY_TABLES:
        raise HTTPException(
            status_code=422, detail=f"Ungültige Kategorie. Erlaubt: {', '.join(sorted(CATEGORY_TABLES))}"
        )
    email = _require_email(authorization)
    rows = _load_category_rows(email, category)
    csv_text = rows_to_csv(rows)

    record_audit_event(
        user_id=None, email=email, action="export_request", entity_type=f"category_csv:{category}"
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="vitaltwin_{category}.csv"'},
    )
