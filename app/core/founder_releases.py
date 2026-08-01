"""Release & Build data model (Founder OS internal foundation #5).

Real data model for "letzter Release" / "Build-Status" — starts completely
EMPTY. Nothing here is auto-detected or guessed: a release only shows up
once a founder/admin (or, later, a real CI/CD webhook) explicitly records it
via `POST /api/admin/system/releases`. Until the first real entry exists,
`get_latest_release()` returns `None` and callers must show "noch keine
Releases erfasst" — never a fabricated version number.
"""

from __future__ import annotations

from .supabase import supabase

TABLE = "vt_founder_releases"

ALLOWED_BUILD_STATUSES = {"erfolgreich", "fehlgeschlagen", "unbekannt"}


def record_release(
    *,
    version: str,
    released_by: str | None = None,
    git_commit_sha: str | None = None,
    environment: str = "production",
    description: str | None = None,
    build_status: str = "unbekannt",
) -> dict[str, object] | None:
    build_status = build_status if build_status in ALLOWED_BUILD_STATUSES else "unbekannt"
    row = {
        "version": version,
        "released_by": released_by,
        "git_commit_sha": git_commit_sha,
        "environment": environment,
        "description": description,
        "build_status": build_status,
    }
    try:
        response = supabase.table(TABLE).insert(row).execute()
    except Exception:
        return None
    return response.data[0] if response.data else None


def get_latest_release() -> dict[str, object] | None:
    try:
        rows = supabase.table(TABLE).select("*").order("released_at", desc=True).limit(1).execute().data or []
    except Exception:
        return None
    return rows[0] if rows else None


def list_releases(*, limit: int = 20) -> list[dict[str, object]]:
    try:
        return (
            supabase.table(TABLE)
            .select("*")
            .order("released_at", desc=True)
            .limit(max(1, min(limit, 100)))
            .execute()
            .data
            or []
        )
    except Exception:
        return []
