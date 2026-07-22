"""Audit logging for Twin Intelligence data.

Twin Intelligence Core — Etappe 2 (§12 Audit-Grundlage).

Writes append-only events to `vt_audit_events` (see
`migrations/003_twin_intelligence_foundation.sql`). Deliberately stores only
structured metadata — never full free-text content, passwords, tokens, or
other secrets — so this table can be reviewed or exported without itself
becoming a privacy liability.
"""

from __future__ import annotations

from typing import Literal

from .supabase import supabase

AUDIT_TABLE = "vt_audit_events"

AuditAction = Literal[
    "create",
    "update",
    "delete",
    "export_request",
    "deletion_request",
    "consent_change",
]


def record_audit_event(
    *,
    user_id: int | None,
    email: str | None,
    action: AuditAction,
    entity_type: str,
    entity_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Best-effort audit write.

    Never raises: a failed audit write must not block the user's actual
    request. Callers should still treat this as "fire and forget" logging,
    not as a transactional guarantee.
    """
    try:
        supabase.table(AUDIT_TABLE).insert(
            {
                "user_id": user_id,
                "email": email,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "metadata": metadata or {},
            }
        ).execute()
    except Exception:
        pass
