"""Central System Event Logging (Founder OS internal foundation #3).

A generic, cross-cutting log for operational/lifecycle events that don't fit
the AI-usage or error-event tables — e.g. process startup, and (future) real
deploy/backup-job webhooks. This is NOT a duplicate of `vt_error_events`
(dedicated, narrower schema for exceptions) or `vt_ai_usage_events`
(dedicated to AI requests) — it's the general-purpose log the other two
intentionally don't try to also cover.
"""

from __future__ import annotations

from .supabase import supabase

TABLE = "vt_system_events"

ALLOWED_SEVERITIES = {"info", "warning", "error", "critical"}


def log_system_event(
    *,
    event_type: str,
    message: str,
    severity: str = "info",
    source: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Writes one row to the central system event log. Never raises."""
    severity = severity if severity in ALLOWED_SEVERITIES else "info"
    row = {
        "event_type": event_type,
        "message": message[:2000],
        "severity": severity,
        "source": source,
        "metadata": metadata or {},
    }
    try:
        supabase.table(TABLE).insert(row).execute()
    except Exception:
        pass


def list_recent_system_events(*, limit: int = 20) -> list[dict[str, object]]:
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
