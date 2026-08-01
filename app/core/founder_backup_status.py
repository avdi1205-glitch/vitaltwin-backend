"""Backup Status data model (Founder OS internal foundation #6).

Real data model for "letzter Backup-Status" — starts completely EMPTY.
Nothing here is auto-detected: no backup job runs today (see
`docs/FOUNDER_OS_MISSING_INTEGRATIONS.md`), so a status only shows up once a
founder/admin (or, later, a real backup job) explicitly records it via
`POST /api/admin/system/backups`. Until the first real entry exists,
`get_latest_backup_status()` returns `None` and callers must show "kein
Backup-System angebunden" — never a fabricated "OK" status.
"""

from __future__ import annotations

from .supabase import supabase

TABLE = "vt_founder_backup_status"

ALLOWED_STATUSES = {"erfolgreich", "fehlgeschlagen", "laeuft"}


def record_backup(
    *,
    status: str,
    backup_type: str = "database",
    size_bytes: int | None = None,
    completed_at: str | None = None,
    note: str | None = None,
    recorded_by: str | None = None,
) -> dict[str, object] | None:
    if status not in ALLOWED_STATUSES:
        return None
    row = {
        "status": status,
        "backup_type": backup_type,
        "size_bytes": size_bytes,
        "completed_at": completed_at,
        "note": note,
        "recorded_by": recorded_by,
    }
    try:
        response = supabase.table(TABLE).insert(row).execute()
    except Exception:
        return None
    return response.data[0] if response.data else None


def get_latest_backup_status() -> dict[str, object] | None:
    try:
        rows = (
            supabase.table(TABLE)
            .select("*")
            .order("completed_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        return None
    return rows[0] if rows else None


def list_backups(*, limit: int = 20) -> list[dict[str, object]]:
    try:
        return (
            supabase.table(TABLE)
            .select("*")
            .order("completed_at", desc=True)
            .limit(max(1, min(limit, 100)))
            .execute()
            .data
            or []
        )
    except Exception:
        return []
