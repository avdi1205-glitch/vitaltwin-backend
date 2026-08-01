"""Central Error Event Logging (Founder OS internal foundation #7).

Every unhandled backend exception (caught by the global exception handler in
`app/main.py`) writes exactly one row here. This is real "Error Tracking" —
scoped honestly: it only ever sees exceptions that actually escaped a route
handler in THIS process. It is not a replacement for an external tool like
Sentry (no source maps, no stack-trace grouping, no alerting/paging) — see
`docs/FOUNDER_OS_MISSING_INTEGRATIONS.md` for what a real Sentry integration
would additionally require.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .supabase import supabase

TABLE = "vt_error_events"


def log_error_event(
    *,
    source: str,
    error_type: str,
    message: str,
    email: str | None = None,
    request_path: str | None = None,
) -> None:
    """Writes one row to the central error log. Never raises."""
    row = {
        "source": source,
        "error_type": error_type,
        "message": message[:2000],
        "request_path": request_path,
        "email": email,
    }
    try:
        supabase.table(TABLE).insert(row).execute()
    except Exception:
        pass


def get_error_summary(*, days: int = 7) -> dict[str, object]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = (
            supabase.table(TABLE)
            .select("error_type,created_at")
            .gte("created_at", since)
            .execute()
            .data
            or []
        )
    except Exception:
        return {"total": None, "by_type": None, "note": "Nicht verfügbar — vt_error_events nicht erreichbar."}

    by_type: dict[str, int] = {}
    for row in rows:
        error_type = row.get("error_type") or "unbekannt"
        by_type[error_type] = by_type.get(error_type, 0) + 1

    return {"total": len(rows), "by_type": by_type, "note": None}


def list_recent_errors(*, limit: int = 20) -> list[dict[str, object]]:
    try:
        return (
            supabase.table(TABLE)
            .select("*")
            .order("created_at", desc=True)
            .limit(max(1, min(limit, 200)))
            .execute()
            .data
            or []
        )
    except Exception:
        return []
