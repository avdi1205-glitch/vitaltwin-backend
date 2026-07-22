"""Twin Learning Event logging.

Twin Intelligence Core — Etappe 5 §4.

Writes structured, append-only events to `vt_twin_learning_events` (see
`migrations/006_twin_memory_patterns_learning.sql`). Distinct from
`core/audit.py::record_audit_event`: audit events are a compliance/security
log ("who changed what, when"), while Twin Learning Events are the Twin's own
record of *why* it changed its mind about something (a preference recognized,
a pattern discarded, a memory corrected, ...) — the raw material a future
"Weekly Reflection"/"Monthly Progress" etappe reads to explain its own
behaviour to the user.

Deliberately stores only small, structured `previous_state`/`new_state`
snippets — never a full free-text dump (§4: "Keine unnötigen vollständigen
Freitexte speichern") — and a short `reason` string, not an essay.
"""

from __future__ import annotations

from .supabase import supabase

LEARNING_EVENT_TABLE = "vt_twin_learning_events"


def record_learning_event(
    *,
    user_id: int | None,
    email: str | None,
    event_type: str,
    source_type: str,
    source_id: str | None = None,
    previous_state: dict[str, object] | None = None,
    new_state: dict[str, object],
    reason: str | None = None,
) -> None:
    """Best-effort write: never raises. A failed learning-event write must
    never block the actual request that triggered it (identical contract to
    `record_audit_event`)."""
    try:
        supabase.table(LEARNING_EVENT_TABLE).insert(
            {
                "user_id": user_id,
                "email": email,
                "event_type": event_type,
                "source_type": source_type,
                "source_id": source_id,
                "previous_state": previous_state,
                "new_state": new_state,
                "reason": reason,
            }
        ).execute()
    except Exception:
        pass
