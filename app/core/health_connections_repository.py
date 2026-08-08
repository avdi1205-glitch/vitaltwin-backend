"""Repository for `user_health_connections` — the single source of truth for
"is this user connected to Google Health, and with which tokens/scopes".

Includes the best-effort, documented-limitation single-flight lock used by
`health_token_service.py` to reduce (not eliminate) concurrent-refresh races.
supabase-py only exposes the PostgREST table API (no raw SQL connection), so
a true Postgres advisory lock (`pg_advisory_lock`) is not reachable from this
codebase's DB access layer — this uses a conditional UPDATE instead, which is
"good enough" for the current single-instance Railway deployment (same
documented limitation as `core/rate_limit.py`'s in-memory buckets) but is NOT
a hard atomicity guarantee under true concurrent multi-instance load.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from .supabase import supabase

CONNECTION_TABLE = "user_health_connections"
PROVIDER = "google_health"

ACTIVE_STATUSES = ("connected", "reauthorization_required")


def get_active_connection(user_id: int) -> dict[str, object] | None:
    rows = (
        supabase.table(CONNECTION_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("provider", PROVIDER)
        .in_("status", list(ACTIVE_STATUSES))
        .order("id", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def get_any_connection(user_id: int) -> dict[str, object] | None:
    rows = (
        supabase.table(CONNECTION_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("provider", PROVIDER)
        .order("id", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def get_connection_by_id(connection_id: int) -> dict[str, object] | None:
    rows = supabase.table(CONNECTION_TABLE).select("*").eq("id", connection_id).limit(1).execute().data or []
    return rows[0] if rows else None


def upsert_connection(
    *,
    user_id: int,
    encrypted_access_token: str,
    encrypted_refresh_token: str,
    access_token_expires_at: str,
    granted_scopes: list[str],
    provider_health_user_id: str | None,
    provider_legacy_user_id: str | None,
) -> dict[str, object]:
    """Creates a new connection row, or reuses/overwrites an existing one for
    this user+provider (whatever its previous status) — there is exactly one
    logical connection per user+provider, enforced by the partial unique
    index on `(user_id, provider)` for active statuses."""
    existing = get_any_connection(user_id)
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "user_id": user_id,
        "provider": PROVIDER,
        "provider_health_user_id": provider_health_user_id,
        "provider_legacy_user_id": provider_legacy_user_id,
        "encrypted_access_token": encrypted_access_token,
        "encrypted_refresh_token": encrypted_refresh_token,
        "access_token_expires_at": access_token_expires_at,
        "granted_scopes": granted_scopes,
        "status": "connected",
        "connected_at": now,
        "reauthorization_required_at": None,
        "reauthorization_reason": None,
        "updated_at": now,
    }
    if existing:
        supabase.table(CONNECTION_TABLE).update(row).eq("id", existing["id"]).execute()
        return get_connection_by_id(existing["id"])  # type: ignore[return-value]
    result = supabase.table(CONNECTION_TABLE).insert(row).execute()
    return result.data[0]


def update_tokens(
    connection_id: int,
    *,
    encrypted_access_token: str,
    encrypted_refresh_token: str,
    access_token_expires_at: str,
) -> None:
    supabase.table(CONNECTION_TABLE).update(
        {
            "encrypted_access_token": encrypted_access_token,
            "encrypted_refresh_token": encrypted_refresh_token,
            "access_token_expires_at": access_token_expires_at,
            "status": "connected",
            "reauthorization_required_at": None,
            "reauthorization_reason": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", connection_id).execute()


def mark_reauthorization_required(connection_id: int, reason: str) -> None:
    supabase.table(CONNECTION_TABLE).update(
        {
            "status": "reauthorization_required",
            "reauthorization_required_at": datetime.now(timezone.utc).isoformat(),
            "reauthorization_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", connection_id).execute()


def mark_sync_result(
    connection_id: int,
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    supabase.table(CONNECTION_TABLE).update(
        {
            "last_sync_at": datetime.now(timezone.utc).isoformat(),
            "last_sync_status": status,
            "last_sync_error_code": error_code,
            "last_sync_error_message": error_message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", connection_id).execute()


def disconnect_connection(connection_id: int) -> None:
    """Real disconnection: tokens are deleted (not just marked inactive) —
    no lingering encrypted secrets left behind for a status='disconnected'
    row, per the explicit "Tokens löschen" requirement."""
    supabase.table(CONNECTION_TABLE).update(
        {
            "status": "disconnected",
            "encrypted_access_token": "",
            "encrypted_refresh_token": "",
            "refresh_lock_token": None,
            "refresh_lock_expires_at": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", connection_id).execute()


# ---------------------------------------------------------------------------
# Best-effort single-flight refresh lock (see module docstring)
# ---------------------------------------------------------------------------


def try_acquire_refresh_lock(connection_id: int, *, ttl_seconds: int = 30) -> str | None:
    lock_token = secrets.token_hex(8)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

    updated = (
        supabase.table(CONNECTION_TABLE)
        .update({"refresh_lock_token": lock_token, "refresh_lock_expires_at": expires_at})
        .eq("id", connection_id)
        .or_(f"refresh_lock_expires_at.is.null,refresh_lock_expires_at.lt.{now.isoformat()}")
        .execute()
    )
    acquired_rows = updated.data or []
    if acquired_rows and acquired_rows[0].get("refresh_lock_token") == lock_token:
        return lock_token
    return None


def release_refresh_lock(connection_id: int, lock_token: str) -> None:
    supabase.table(CONNECTION_TABLE).update(
        {"refresh_lock_token": None, "refresh_lock_expires_at": None}
    ).eq("id", connection_id).eq("refresh_lock_token", lock_token).execute()
