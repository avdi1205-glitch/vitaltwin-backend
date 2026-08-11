"""Twin Memory, Pattern Detection, and Twin Learning Events.

Twin Intelligence Core — Etappe 5.

Endpoints (mounted at `/api/memory` in `app/main.py`):

- `GET  /`                          list the user's memories (all
                                     non-deleted statuses), generating new
                                     rule-based candidates from their own
                                     recent data first (§1-2).
- `POST /`                          explicitly store a user-stated memory
                                     (`persoenliche_regel` /
                                     `bevorzugte_kommunikationsform` — the two
                                     types that can only ever come from an
                                     explicit user statement, never be
                                     inferred).
- `POST /{id}/confirm`              "Memory bestätigen" (§2).
- `POST /{id}/correct`              "Memory korrigieren" (§2).
- `POST /{id}/reject`               "Memory ablehnen" (§2).
- `POST /{id}/archive`              "Memory archivieren" (§2).
- `DELETE /{id}`                    "Memory löschen" (§2) — re-evaluates
                                     dependent candidates of the same type.
- `GET  /patterns`                  list active (non-discarded) patterns,
                                     generating new ones from recent data
                                     first (§3).
- `POST /patterns/{id}/discard`     "Pattern verwerfen" (§3).

Nutzertrennung: every endpoint resolves `email` server-side from the session
token (`core/auth.py`) and every lookup is scoped `.eq("email", email)` — a
manipulated/guessed id can never return another user's memory or pattern
(404, not 403, see `core/auth.py`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.auth import require_email as _require_email_dependency
from ..core.auth import get_user_id_by_email
from ..core.concurrency import run_parallel
from ..core.learning_events import record_learning_event
from ..core.supabase import supabase
from ..core.validation import MAX_MEMORY_REASON, MAX_SHORT_TEXT, validate_short_text
from ..services import pattern_detection, twin_memory
from ..services.daily_signal_view import build_daily_signals
from ..services.habit_service import compute_habit_stats
from ..services.twin_learning_timeline import DEFAULT_LIMIT, MAX_LIMIT, build_learning_timeline

router = APIRouter()

MEMORY_TABLE = "vt_twin_memory"
PATTERN_TABLE = "vt_twin_patterns"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
HABIT_TABLE = "vt_habits"
HABIT_ENTRY_TABLE = "vt_habit_entries"
GOAL_TABLE = "vt_wellness_goals"
RECOMMENDATION_TABLE = "vt_recommendations"
HEALTH_ACTIVITY_TABLE = "health_activity_records"
CGM_TABLE = "vt_cgm_readings"
NUTRITION_TABLE = "vt_nutrition_entries"
LEARNING_EVENT_TABLE = "vt_twin_learning_events"
CROSS_DOMAIN_LOOKBACK_DAYS = 30
"""Twin Core Phase 3 — matches `pattern_detection.LOOKBACK_DAYS`, so the
daily signal view covers exactly the window the correlation detectors
actually look at."""

MANUAL_MEMORY_TYPES = {"persoenliche_regel", "bevorzugte_kommunikationsform"}
"""Die einzigen zwei Memory-Typen, die ein Nutzer direkt und ausdrücklich
anlegen darf (Etappe 5 §1: "ausdrücklich gespeicherte persönliche Regel",
"bevorzugte Kommunikationsform") — alle anderen sechs Typen entstehen
ausschließlich aus Beobachtung (`services/twin_memory.py`-Detektoren)."""

NON_RESURRECTABLE_STATUSES = {"disputed", "archived", "deleted"}
"""Eine automatische Neu-Erkennung darf eine Memory/ein Pattern nie aus
einem dieser Zustände zurückholen — eine explizite Nutzerentscheidung wiegt
schwerer als eine erneute Beobachtung (Etappe 5 §5)."""


def _require_email(authorization: str | None) -> str:
    return _require_email_dependency(authorization)


class ManualMemoryInput(BaseModel):
    memory_type: str
    title: str
    human_readable_value: str
    normalized_value: dict[str, object] | None = None

    @field_validator("memory_type")
    @classmethod
    def _validate_memory_type(cls, value: str) -> str:
        if value not in MANUAL_MEMORY_TYPES:
            raise ValueError(
                f"Nur folgende Typen können direkt gespeichert werden: {', '.join(sorted(MANUAL_MEMORY_TYPES))}"
            )
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        cleaned = validate_short_text(value, field_name="Titel", max_length=MAX_SHORT_TEXT)
        if not cleaned:
            raise ValueError("Titel darf nicht leer sein.")
        return cleaned

    @field_validator("human_readable_value")
    @classmethod
    def _validate_human_readable_value(cls, value: str) -> str:
        cleaned = validate_short_text(value, field_name="Inhalt", max_length=MAX_SHORT_TEXT)
        if not cleaned:
            raise ValueError("Inhalt darf nicht leer sein.")
        return cleaned


class MemoryCorrectionInput(BaseModel):
    human_readable_value: str
    normalized_value: dict[str, object] | None = None
    reason: str | None = None

    @field_validator("human_readable_value")
    @classmethod
    def _validate_human_readable_value(cls, value: str) -> str:
        cleaned = validate_short_text(value, field_name="Inhalt", max_length=MAX_SHORT_TEXT)
        if not cleaned:
            raise ValueError("Inhalt darf nicht leer sein.")
        return cleaned

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Grund", max_length=MAX_MEMORY_REASON)


class MemoryActionInput(BaseModel):
    """Shared body for confirm/reject/archive/discard — a short, optional
    reason only, never a full free-text essay (see Etappe 5 §4)."""

    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        return validate_short_text(value, field_name="Grund", max_length=MAX_MEMORY_REASON)


def _require_own_memory(email: str, memory_id: str) -> dict[str, object]:
    try:
        response = (
            supabase.table(MEMORY_TABLE)
            .select("*")
            .eq("id", memory_id)
            .eq("email", email)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Memory konnte nicht geladen werden.") from exc
    if not response.data:
        raise HTTPException(status_code=404, detail="Memory nicht gefunden.")
    return response.data[0]


def _require_own_pattern(email: str, pattern_id: str) -> dict[str, object]:
    try:
        response = (
            supabase.table(PATTERN_TABLE).select("*").eq("id", pattern_id).eq("email", email).limit(1).execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Muster konnte nicht geladen werden.") from exc
    if not response.data:
        raise HTTPException(status_code=404, detail="Muster nicht gefunden.")
    return response.data[0]


_PREFERENCE_LIKE_TYPES = {
    "bestaetigte_praeferenz",
    "bevorzugte_aktivitaetszeit",
    "erfolgreiche_routine",
    "bevorzugte_kommunikationsform",
}


def _creation_event_type(memory_type: str) -> str:
    if memory_type in _PREFERENCE_LIKE_TYPES:
        return "praeferenz_erkannt"
    if memory_type == "bestaetigtes_muster":
        return "muster_erkannt"
    return "memory_erstellt"


def _load_habits_raw(email: str) -> list[dict[str, object]]:
    try:
        return supabase.table(HABIT_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        return []


def _load_habit_entries(email: str) -> list[dict[str, object]]:
    try:
        return (
            supabase.table(HABIT_ENTRY_TABLE)
            .select("habit_id,entry_date,completed")
            .eq("email", email)
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def _combine_habits_with_stats(
    habits_raw: list[dict[str, object]], habit_entries: list[dict[str, object]], today: date
) -> list[dict[str, object]]:
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
async def list_memories(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()
    now = datetime.now(timezone.utc)

    def _goals() -> list[dict[str, object]]:
        try:
            return (
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
            return []

    def _recommendation_history() -> list[dict[str, object]]:
        try:
            return supabase.table(RECOMMENDATION_TABLE).select("*").eq("email", email).limit(200).execute().data or []
        except Exception:
            return []

    def _confirmed_patterns() -> list[dict[str, object]]:
        try:
            return (
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
            return []

    def _existing_memories() -> list[dict[str, object]]:
        try:
            return (
                supabase.table(MEMORY_TABLE).select("*").eq("email", email).is_("deleted_at", "null").execute().data
                or []
            )
        except Exception:
            return []

    goals, habits_raw, habit_entries, recommendation_history, confirmed_patterns, existing_memories = run_parallel(
        _goals, lambda: _load_habits_raw(email), lambda: _load_habit_entries(email),
        _recommendation_history, _confirmed_patterns, _existing_memories,
    )
    habits = _combine_habits_with_stats(habits_raw, habit_entries, today)
    existing_by_key = {row.get("memory_key"): row for row in existing_memories}

    candidates = twin_memory.generate_memory_candidates(
        habits=habits,
        goals=goals,
        recommendation_history=recommendation_history,
        confirmed_patterns=confirmed_patterns,
        today=today,
    )

    today_marker = f"observed:{today.isoformat()}"
    result_rows: list[dict[str, object]] = list(existing_memories)

    for candidate in candidates:
        existing = existing_by_key.get(candidate.memory_key)

        if existing is None:
            payload = {
                "email": email,
                "memory_key": candidate.memory_key,
                "memory_type": candidate.memory_type,
                "title": candidate.title,
                "normalized_value": candidate.normalized_value,
                "human_readable_value": candidate.human_readable_value,
                "source": candidate.source,
                "source_references": [*candidate.source_references, today_marker],
                "confidence": candidate.confidence,
                "status": "candidate",
                "user_confirmed": False,
                "first_observed_at": now.isoformat(),
                "last_used_at": now.isoformat(),
            }
            try:
                response = supabase.table(MEMORY_TABLE).insert(payload).execute()
            except Exception:
                continue
            if response.data:
                row = response.data[0]
                result_rows.append(row)
                record_learning_event(
                    user_id=None,
                    email=email,
                    event_type=_creation_event_type(candidate.memory_type),
                    source_type="twin_memory",
                    source_id=str(row.get("id")),
                    new_state={"memory_type": candidate.memory_type, "status": "candidate", "confidence": candidate.confidence},
                    reason=candidate.human_readable_value,
                )
            continue

        if existing.get("status") in NON_RESURRECTABLE_STATUSES:
            # Respect the user's explicit decision — do not silently
            # regenerate/resurface it just because the pattern re-occurred.
            continue

        references = list(existing.get("source_references") or [])
        if today_marker not in references:
            references.append(today_marker)
        observation_count = sum(1 for ref in references if str(ref).startswith("observed:"))
        new_status = twin_memory.promote_after_observation(
            str(existing.get("status")), observation_count=observation_count
        )
        updates: dict[str, object] = {
            "source_references": references,
            "last_used_at": now.isoformat(),
            "confidence": max(float(existing.get("confidence") or 0), candidate.confidence),
        }
        if new_status != existing.get("status"):
            updates["status"] = new_status
        try:
            supabase.table(MEMORY_TABLE).update(updates).eq("id", existing["id"]).eq("email", email).execute()
        except Exception:
            continue
        merged = {**existing, **updates}
        for idx, row in enumerate(result_rows):
            if row.get("id") == existing.get("id"):
                result_rows[idx] = merged
                break
        if new_status != existing.get("status"):
            record_learning_event(
                user_id=None,
                email=email,
                event_type="praeferenz_bestaetigt",
                source_type="twin_memory",
                source_id=str(existing.get("id")),
                previous_state={"status": existing.get("status"), "confidence": existing.get("confidence")},
                new_state={"status": new_status, "confidence": updates.get("confidence")},
                reason="Wiederholt beobachtet",
            )

    return {"items": result_rows}


@router.post("")
async def create_manual_memory(data: ManualMemoryInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    now = datetime.now(timezone.utc)
    payload = {
        "email": email,
        "memory_key": f"manual:{data.memory_type}:{now.timestamp()}",
        "memory_type": data.memory_type,
        "title": data.title,
        "normalized_value": data.normalized_value or {},
        "human_readable_value": data.human_readable_value,
        "source": "user_reported",
        "source_references": ["user_statement"],
        # Ein ausdrücklich vom Nutzer selbst formulierter Satz ist keine vom
        # Twin abgeleitete Beobachtung — er darf direkt als bestätigt gelten
        # (Etappe 5 §1 gilt für vom Twin *beobachtete* Muster, nicht für vom
        # Nutzer selbst diktierte Regeln).
        "confidence": 0.95,
        "status": "confirmed",
        "user_confirmed": True,
        "first_observed_at": now.isoformat(),
        "last_confirmed_at": now.isoformat(),
        "last_used_at": now.isoformat(),
    }
    try:
        response = supabase.table(MEMORY_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Memory konnte nicht gespeichert werden.") from exc

    row = response.data[0] if response.data else payload
    record_learning_event(
        user_id=None,
        email=email,
        event_type="memory_erstellt",
        source_type="twin_memory",
        source_id=str(row.get("id")),
        new_state={"memory_type": data.memory_type, "status": "confirmed"},
        reason=data.human_readable_value,
    )
    return row


@router.post("/{memory_id}/confirm")
async def confirm_memory(memory_id: str, data: MemoryActionInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    memory = _require_own_memory(email, memory_id)
    now = datetime.now(timezone.utc)

    updates = twin_memory.apply_user_confirmation(memory, now=now)
    try:
        supabase.table(MEMORY_TABLE).update(updates).eq("id", memory_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Memory konnte nicht bestätigt werden.") from exc

    record_learning_event(
        user_id=None,
        email=email,
        event_type="memory_bestaetigt",
        source_type="twin_memory",
        source_id=memory_id,
        previous_state={"status": memory.get("status"), "confidence": memory.get("confidence")},
        new_state={"status": "confirmed", "confidence": updates.get("confidence")},
        reason=data.reason,
    )
    return {**memory, **updates}


@router.post("/{memory_id}/correct")
async def correct_memory(memory_id: str, data: MemoryCorrectionInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    memory = _require_own_memory(email, memory_id)
    now = datetime.now(timezone.utc)

    updates = twin_memory.apply_user_correction(
        memory, human_readable_value=data.human_readable_value, normalized_value=data.normalized_value, now=now
    )
    try:
        supabase.table(MEMORY_TABLE).update(updates).eq("id", memory_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Memory konnte nicht korrigiert werden.") from exc

    record_learning_event(
        user_id=None,
        email=email,
        event_type="memory_korrigiert",
        source_type="twin_memory",
        source_id=memory_id,
        previous_state={"human_readable_value": memory.get("human_readable_value"), "confidence": memory.get("confidence")},
        new_state={"human_readable_value": data.human_readable_value, "confidence": updates.get("confidence")},
        reason=data.reason,
    )
    return {**memory, **updates}


@router.post("/{memory_id}/reject")
async def reject_memory(memory_id: str, data: MemoryActionInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    memory = _require_own_memory(email, memory_id)
    now = datetime.now(timezone.utc)

    updates = twin_memory.apply_user_rejection(memory, now=now)
    try:
        supabase.table(MEMORY_TABLE).update(updates).eq("id", memory_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Memory konnte nicht abgelehnt werden.") from exc

    record_learning_event(
        user_id=None,
        email=email,
        event_type="memory_abgelehnt",
        source_type="twin_memory",
        source_id=memory_id,
        previous_state={"status": memory.get("status"), "confidence": memory.get("confidence")},
        new_state={"status": "disputed", "confidence": updates.get("confidence")},
        reason=data.reason,
    )
    return {**memory, **updates}


@router.post("/{memory_id}/archive")
async def archive_memory(memory_id: str, data: MemoryActionInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    memory = _require_own_memory(email, memory_id)
    now = datetime.now(timezone.utc)

    updates = twin_memory.apply_archive(now)
    try:
        supabase.table(MEMORY_TABLE).update(updates).eq("id", memory_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Memory konnte nicht archiviert werden.") from exc

    record_learning_event(
        user_id=None,
        email=email,
        event_type="memory_archiviert",
        source_type="twin_memory",
        source_id=memory_id,
        previous_state={"status": memory.get("status")},
        new_state={"status": "archived"},
        reason=data.reason,
    )
    return {**memory, **updates}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, authorization: str | None = Header(default=None)):
    """Etappe 5 §2: nach Löschung darf die Memory nicht mehr im KI-Kontext
    oder für Empfehlungen verwendet werden (`deleted_at` gesetzt,
    `status="deleted"` — beides wird von
    `twin_memory.is_usable_for_recommendations` respektiert), und abhängige
    Kandidaten desselben Typs werden neu bewertet."""
    email = _require_email(authorization)
    memory = _require_own_memory(email, memory_id)
    now = datetime.now(timezone.utc)

    updates = twin_memory.apply_deletion(now)
    try:
        supabase.table(MEMORY_TABLE).update(updates).eq("id", memory_id).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Memory konnte nicht gelöscht werden.") from exc

    record_learning_event(
        user_id=None,
        email=email,
        event_type="memory_geloescht",
        source_type="twin_memory",
        source_id=memory_id,
        previous_state={"status": memory.get("status")},
        new_state={"status": "deleted"},
        reason=None,
    )

    try:
        other_memories = (
            supabase.table(MEMORY_TABLE)
            .select("*")
            .eq("email", email)
            .is_("deleted_at", "null")
            .neq("id", memory_id)
            .execute()
            .data
            or []
        )
    except Exception:
        other_memories = []

    dependent_updates = twin_memory.reevaluate_dependent_candidates(
        str(memory.get("memory_type")), other_memories, now=now
    )
    for dependent_id, dependent_payload in dependent_updates:
        try:
            supabase.table(MEMORY_TABLE).update(dependent_payload).eq("id", dependent_id).eq("email", email).execute()
        except Exception:
            continue

    return {"message": "Memory gelöscht.", "reevaluated_count": len(dependent_updates)}


# ---------------------------------------------------------------------------
# Pattern Detection (Etappe 5 §3)
# ---------------------------------------------------------------------------


@router.get("/patterns")
async def list_patterns(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    today = date.today()
    now = datetime.now(timezone.utc)

    def _daily_entries() -> list[dict[str, object]]:
        try:
            return (
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
            return []

    def _habit_entries() -> list[dict[str, object]]:
        try:
            return (
                supabase.table(HABIT_ENTRY_TABLE)
                .select("habit_id,entry_date,completed")
                .eq("email", email)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _recommendation_history() -> list[dict[str, object]]:
        try:
            return supabase.table(RECOMMENDATION_TABLE).select("*").eq("email", email).limit(200).execute().data or []
        except Exception:
            return []

    def _existing_patterns() -> list[dict[str, object]]:
        try:
            return supabase.table(PATTERN_TABLE).select("*").eq("email", email).execute().data or []
        except Exception:
            return []

    def _user_id() -> int | None:
        # Twin Core Phase 3: Google Health tables are keyed by `user_id`,
        # not `email` — reuses the EXISTING core/auth.py resolver, no
        # second identity system (Step 11: mixed email/user_id sources
        # must still resolve to the SAME authenticated user only).
        return get_user_id_by_email(email)

    def _cgm_rows() -> list[dict[str, object]]:
        since_iso = (today - timedelta(days=CROSS_DOMAIN_LOOKBACK_DAYS)).isoformat()
        try:
            return (
                supabase.table(CGM_TABLE)
                .select("glucose_value,reading_at")
                .eq("email", email)
                .gte("reading_at", since_iso)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _nutrition_rows() -> list[dict[str, object]]:
        since_iso = (today - timedelta(days=CROSS_DOMAIN_LOOKBACK_DAYS)).isoformat()
        try:
            return (
                supabase.table(NUTRITION_TABLE)
                .select("carbs,logged_at")
                .eq("email", email)
                .gte("logged_at", since_iso)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    (
        daily_entries,
        habit_entries,
        recommendation_history,
        existing_patterns,
        user_id,
        cgm_rows,
        nutrition_rows,
    ) = run_parallel(
        _daily_entries, _habit_entries, _recommendation_history, _existing_patterns, _user_id, _cgm_rows, _nutrition_rows
    )

    def _google_steps_rows() -> list[dict[str, object]]:
        if user_id is None:
            return []
        since_iso = (today - timedelta(days=CROSS_DOMAIN_LOOKBACK_DAYS)).isoformat()
        try:
            return (
                supabase.table(HEALTH_ACTIVITY_TABLE)
                .select("start_time,value")
                .eq("user_id", user_id)
                .eq("data_type", "steps")
                .gte("start_time", since_iso)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    google_steps_rows = _google_steps_rows()

    daily_signals = build_daily_signals(
        checkin_entries=daily_entries,
        google_steps_rows=google_steps_rows,
        cgm_rows=cgm_rows,
        nutrition_rows=nutrition_rows,
        today=today,
        window_days=CROSS_DOMAIN_LOOKBACK_DAYS,
    )

    existing_by_key = {row.get("pattern_key"): row for row in existing_patterns}

    drafts = pattern_detection.generate_patterns(
        daily_entries=daily_entries,
        habit_entries=habit_entries,
        recommendation_history=recommendation_history,
        today=today,
        daily_signals=daily_signals,
    )

    result_rows: list[dict[str, object]] = [row for row in existing_patterns if row.get("status") != "discarded"]

    for draft in drafts:
        existing = existing_by_key.get(draft.pattern_key)
        if existing is not None and existing.get("status") == "discarded":
            continue

        payload = {
            "email": email,
            "pattern_key": draft.pattern_key,
            "pattern_type": draft.pattern_type,
            "variables": list(draft.variables),
            "summary": draft.summary,
            "description": draft.summary,
            "period_days": draft.period_days,
            "data_points": draft.data_points,
            "confidence": draft.confidence,
            "data_quality": draft.data_quality,
            "status": "active",
            "contradicting": draft.contradicting,
            "evidence": draft.evidence,
            "updated_at": now.isoformat(),
        }

        if existing is None:
            try:
                response = supabase.table(PATTERN_TABLE).insert(payload).execute()
            except Exception:
                continue
            if response.data:
                row = response.data[0]
                result_rows.append(row)
                record_learning_event(
                    user_id=None,
                    email=email,
                    event_type="muster_erkannt",
                    source_type="twin_pattern",
                    source_id=str(row.get("id")),
                    new_state={"pattern_type": draft.pattern_type, "confidence": draft.confidence},
                    reason=draft.summary,
                )
        else:
            was_contradicting = bool(existing.get("contradicting"))
            try:
                supabase.table(PATTERN_TABLE).update(payload).eq("id", existing["id"]).eq("email", email).execute()
            except Exception:
                continue
            merged = {**existing, **payload}
            for idx, row in enumerate(result_rows):
                if row.get("id") == existing.get("id"):
                    result_rows[idx] = merged
                    break
            if draft.contradicting and not was_contradicting:
                # Twin Core Phase 6 Part A: record the ONE genuine
                # "not contradicting -> contradicting" transition. Guarded by
                # `was_contradicting` so re-running this loop on every GET
                # (existing behavior) never re-fires once already recorded.
                # No new pattern state, no second event on an unchanged
                # re-read. The reverse ("contradicting" -> supported again)
                # is deliberately NOT given its own event — `contradicting`
                # can already flip back on its own via the next recompute,
                # and the existing lifecycle has no "resurrection" concept
                # to attach a second event type to.
                record_learning_event(
                    user_id=None,
                    email=email,
                    event_type="muster_widerspruch_erkannt",
                    source_type="twin_pattern",
                    source_id=str(existing.get("id")),
                    previous_state={"contradicting": was_contradicting, "confidence": existing.get("confidence")},
                    new_state={"contradicting": True, "confidence": draft.confidence},
                    reason=draft.summary,
                )

    return {"items": result_rows}


@router.post("/patterns/{pattern_id}/discard")
async def discard_pattern(pattern_id: str, data: MemoryActionInput, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    pattern = _require_own_pattern(email, pattern_id)
    now = datetime.now(timezone.utc)

    try:
        supabase.table(PATTERN_TABLE).update({"status": "discarded", "updated_at": now.isoformat()}).eq(
            "id", pattern_id
        ).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Muster konnte nicht verworfen werden.") from exc

    record_learning_event(
        user_id=None,
        email=email,
        event_type="muster_verworfen",
        source_type="twin_pattern",
        source_id=pattern_id,
        previous_state={"status": pattern.get("status")},
        new_state={"status": "discarded"},
        reason=data.reason,
    )
    return {"message": "Muster verworfen."}


@router.get("/learning-timeline")
async def get_learning_timeline(
    authorization: str | None = Header(default=None),
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
):
    """Twin Core Phase 5 — "Was dein Twin über dich gelernt hat". Reads the
    already-persisted `vt_twin_learning_events` table (written from
    `routers/twin_memory.py`/`profile.py`/`recommendations.py`, see
    `services/twin_learning_timeline.py` module docstring for the full
    inventory) and composes a small, deterministic, customer-safe timeline —
    never a raw event dump. Strictly scoped to the requesting user's own
    email; Family membership grants no access to another member's timeline
    (no Family table is touched anywhere in this path)."""
    email = _require_email(authorization)
    query_limit = DEFAULT_LIMIT if limit <= 0 else min(limit, MAX_LIMIT)
    query_offset = max(offset, 0)

    try:
        response = (
            supabase.table(LEARNING_EVENT_TABLE)
            .select("id,event_type,source_type,source_id,previous_state,new_state,reason,created_at")
            .eq("email", email)
            .order("created_at", desc=True)
            .range(query_offset, query_offset + query_limit - 1)
            .execute()
        )
        rows = response.data or []
    except Exception:
        rows = []

    memory_ids = [str(row["source_id"]) for row in rows if row.get("source_type") == "twin_memory" and row.get("source_id")]
    pattern_ids = [str(row["source_id"]) for row in rows if row.get("source_type") == "twin_pattern" and row.get("source_id")]
    goal_ids = [str(row["source_id"]) for row in rows if row.get("source_type") == "wellness_goal" and row.get("source_id")]

    def _current_rows_by_id(table: str, ids: list[str]) -> dict[str, dict[str, object]]:
        if not ids:
            return {}
        try:
            current = supabase.table(table).select("id,status").eq("email", email).in_("id", ids).execute().data or []
        except Exception:
            current = []
        return {str(item["id"]): item for item in current}

    current_memories, current_patterns, current_goals = run_parallel(
        lambda: _current_rows_by_id(MEMORY_TABLE, memory_ids),
        lambda: _current_rows_by_id(PATTERN_TABLE, pattern_ids),
        lambda: _current_rows_by_id(GOAL_TABLE, goal_ids),
    )

    timeline = build_learning_timeline(
        rows,
        source_ids_by_domain={"memory": current_memories, "pattern": current_patterns, "goal": current_goals},
    )
    return {
        "items": [
            {
                "id": entry.id,
                "occurred_at": entry.occurred_at,
                "category": entry.category,
                "related_domain": entry.related_domain,
                "title": entry.title,
                "summary": entry.summary,
                "confidence_before": entry.confidence_before,
                "confidence_after": entry.confidence_after,
                "current_status": entry.current_status,
                "is_current": entry.is_current,
            }
            for entry in timeline
        ],
        "limit": query_limit,
        "offset": query_offset,
        "has_more": len(rows) == query_limit,
    }
