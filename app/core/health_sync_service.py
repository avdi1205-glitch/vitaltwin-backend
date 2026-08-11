"""Sync orchestration — for each requested (or default-priority) data type:
checks the connection actually was granted the required scope, fetches all
pages from Google (bounded by `MAX_PAGES_PER_SYNC`), normalizes each point,
upserts it into the correct table (dedup on `(user_id, data_type,
provider_record_name)` / `(user_id, provider_record_name)` for sleep), and
records real counters on a `health_sync_runs` row — never fabricated.

Sync window: a first-ever sync fetches the last `HEALTH_INITIAL_SYNC_DAYS`
days; every subsequent sync re-fetches from `HEALTH_SYNC_OVERLAP_HOURS`
before the last successful sync (so a data point that arrived late at the
source, e.g. a watch that synced to Google after a delay, is not missed).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import health_connections_repository as repo
from .google_health_client import GoogleHealthClient
from .health_errors import HealthIntegrationError
from .health_normalization_service import SYNC_PRIORITY_ORDER, has_required_scope, normalize_data_point
from .health_token_service import get_valid_access_token
from .supabase import supabase

SYNC_RUN_TABLE = "health_sync_runs"

TABLE_FOR_CATEGORY = {
    "activity": "health_activity_records",
    "sleep": "health_sleep_records",
    "metric": "health_metric_records",
}


def _initial_sync_days() -> int:
    raw = os.getenv("HEALTH_INITIAL_SYNC_DAYS", "").strip()
    try:
        return int(raw) if raw else 30
    except ValueError:
        return 30


def _sync_overlap_hours() -> int:
    raw = os.getenv("HEALTH_SYNC_OVERLAP_HOURS", "").strip()
    try:
        return int(raw) if raw else 48
    except ValueError:
        return 48


def _sync_window_start(connection: dict[str, object]) -> str:
    last_sync_at = connection.get("last_sync_at")
    now = datetime.now(timezone.utc)
    if last_sync_at and isinstance(last_sync_at, str):
        try:
            last = datetime.fromisoformat(last_sync_at.replace("Z", "+00:00"))
            return (last - timedelta(hours=_sync_overlap_hours())).isoformat()
        except ValueError:
            pass
    return (now - timedelta(days=_initial_sync_days())).isoformat()


async def sync_user_health_data(
    *, user_id: int, connection: dict[str, object], requested_data_types: list[str] | None = None
) -> dict[str, object]:
    connection_id = int(connection["id"])  # type: ignore[arg-type]
    data_types = [dt for dt in (requested_data_types or list(SYNC_PRIORITY_ORDER)) if dt in SYNC_PRIORITY_ORDER]
    granted_scopes = connection.get("granted_scopes") or []

    start_time = _sync_window_start(connection)
    end_time = datetime.now(timezone.utc).isoformat()

    run_row = (
        supabase.table(SYNC_RUN_TABLE)
        .insert(
            {
                "user_id": user_id,
                "connection_id": connection_id,
                "provider": "google_health",
                "sync_type": "manual",
                "requested_data_types": data_types,
                "status": "running",
            }
        )
        .execute()
        .data[0]
    )
    run_id = run_row["id"]

    counters = {"received": 0, "created": 0, "updated": 0, "skipped": 0}
    per_type_results: dict[str, object] = {}
    any_failure = False
    any_success = False

    try:
        access_token, connection = await get_valid_access_token(connection)
    except HealthIntegrationError as exc:
        supabase.table(SYNC_RUN_TABLE).update(
            {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_code": exc.code,
                "error_message": exc.message,
            }
        ).eq("id", run_id).execute()
        repo.mark_sync_result(connection_id, status="failed", error_code=exc.code, error_message=exc.message)
        raise

    client = GoogleHealthClient(access_token=access_token)

    for data_type in data_types:
        if not has_required_scope(data_type, granted_scopes):
            per_type_results[data_type] = {"synced": 0, "error_code": "HEALTH_SCOPE_MISSING"}
            continue
        try:
            table_name = TABLE_FOR_CATEGORY[_category_of(data_type)]
            synced = 0
            skip_debug: list[dict[str, object]] = []
            async for item in client.iter_data_points(data_type=data_type, start_time=start_time, end_time=end_time):
                counters["received"] += 1
                normalized = normalize_data_point(data_type, item)
                if not normalized:
                    counters["skipped"] += 1
                    # Root-cause diagnostic: structural keys only (no values,
                    # no tokens/identifiers) — reveals why normalization
                    # rejected this raw point, without logging health data.
                    skip_debug.append({"reason": "normalize_returned_none", "keys": sorted(item.keys())})
                    continue
                normalized["user_id"] = user_id
                normalized["connection_id"] = connection_id
                try:
                    supabase.table(table_name).upsert(
                        normalized,
                        on_conflict=_conflict_columns(data_type),
                    ).execute()
                    counters["created"] += 1
                    synced += 1
                except Exception as upsert_exc:
                    counters["skipped"] += 1
                    skip_debug.append({"reason": "upsert_failed", "detail": str(upsert_exc)[:200]})
            per_type_results[data_type] = {"synced": synced, "error_code": None}
            if skip_debug:
                per_type_results[data_type]["skip_debug"] = skip_debug
            any_success = True
        except HealthIntegrationError as exc:
            # `exc.message` is always our own generated, secret-free string
            # (e.g. "Google Health API Fehler (404).") — surfacing it here
            # is the only safe way to see the real Google HTTP status code
            # for root-cause diagnosis, since it was previously discarded.
            per_type_results[data_type] = {"synced": 0, "error_code": exc.code, "error_message": exc.message}
            any_failure = True

    finished_at = datetime.now(timezone.utc).isoformat()
    if any_failure and any_success:
        status = "partial"
    elif any_failure and not any_success:
        status = "failed"
    else:
        status = "completed"

    supabase.table(SYNC_RUN_TABLE).update(
        {
            "status": status,
            "finished_at": finished_at,
            "records_received": counters["received"],
            "records_created": counters["created"],
            "records_updated": counters["updated"],
            "records_skipped": counters["skipped"],
            "metadata": {"per_type": per_type_results},
        }
    ).eq("id", run_id).execute()

    repo.mark_sync_result(
        connection_id,
        status=status,
        error_code=None if status == "completed" else "HEALTH_SYNC_PARTIAL" if status == "partial" else "HEALTH_SYNC_FAILED",
        error_message=None if status == "completed" else "Ein oder mehrere Datentypen konnten nicht synchronisiert werden.",
    )

    return {"sync_run_id": run_id, "status": status, "counters": counters, "per_type": per_type_results}


def _category_of(data_type: str) -> str:
    from .health_normalization_service import DATA_TYPE_CONFIG

    return DATA_TYPE_CONFIG[data_type].category


def _conflict_columns(data_type: str) -> str:
    category = _category_of(data_type)
    if category == "sleep":
        return "user_id,provider_record_name"
    return "user_id,data_type,provider_record_name"
