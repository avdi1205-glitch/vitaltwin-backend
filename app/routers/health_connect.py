"""Health Connect (Android on-device) — Phase 2.2: full READ-ONLY wellness
data type set (steps, distance, calories, exercise sessions, sleep +
stages, heart rate, resting heart rate, HRV, SpO2, respiratory rate, body
temperature, weight).

Mounted at `/api/health` in `app/main.py` — the SAME top-level prefix as the
existing `routers/health.py` (CGM/nutrition) and `routers/google_health.py`
(cloud OAuth sync). Confirmed collision-free: this file only defines
`/health-connect/sync`.

Deliberately a THIN endpoint, not a second sync engine: it reuses the exact
same 3 canonical tables (`health_activity_records`/`health_metric_records`/
`health_sleep_records`) Google Health already writes into, the same
dedupe/on_conflict strategy, and the same entitlement gate (`google_health`
— Health Connect and Google Health represent the SAME Premium "automatic
health data" capability, just a different data source).

ONE consolidated sync payload for every granted data type (Constitution
rule 17 / task spec section 5) — `records` is a dict keyed by Health
Connect data type, each value a list of raw records in that type's own
shape (as produced by `HealthConnectPlugin.kt`). A category the user never
granted permission for is simply absent from the payload (or an empty
list) — this alone satisfies "permission denial for one category must not
break other categories", no special-casing needed. An unrecognized
`data_type` key is reported in `unsupported_types`, never guessed at.

Health Connect has no OAuth "connection" row (see migration 035) — every
row this endpoint writes has `connection_id = None`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.auth import require_user
from ..core.beta_discount_program import maybe_claim_discount_slot
from ..core.health_normalization_service import (
    HEALTH_CONNECT_TYPES,
    health_connect_conflict_columns,
    health_connect_table_for,
    normalize_health_connect_record,
)
from ..core.plan_service import has_feature
from ..core.supabase import supabase

router = APIRouter()

# Matches Google Health's own health_sync_service.py::SYNC_RUN_TABLE — same
# table, now shared by both providers (migration 041 made connection_id
# nullable specifically so Health Connect rows can use it too).
SYNC_RUN_TABLE = "health_sync_runs"
ALLOWED_SYNC_TYPES = ("manual", "background")


def _require_user_id(authorization: str | None) -> tuple[int, str]:
    """Mirrors `routers/google_health.py::_require_user_id` exactly — same
    identity resolution, same entitlement (Health Connect and Google Health
    are the same Premium "automatic health data" capability). Returns both
    the user_id and email since the discount-program trigger below needs
    the email (that table is email-keyed, matching check-ins/twin-calcs)."""
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")
    if not has_feature(current.email, "google_health"):
        raise HTTPException(
            status_code=403,
            detail="Automatische Gesundheitsdaten sind ein Premium-Feature. Aktiviere Premium unter /preise.",
        )
    return current.user_id, current.email


class HealthConnectSyncRequest(BaseModel):
    # data_type -> list of raw plugin records (shape varies per data type,
    # normalize_health_connect_record() validates/extracts defensively).
    records: dict[str, list[dict[str, object]]] = {}
    # "manual" (button, HealthConnectSync.tsx) or "background" (WorkManager,
    # HealthConnectSyncWorker.kt, Phase 2.3) — purely descriptive, stored in
    # health_sync_runs.sync_type, never affects what gets synced.
    sync_type: str = "manual"

    @field_validator("sync_type")
    @classmethod
    def _validate_sync_type(cls, value: str) -> str:
        if value not in ALLOWED_SYNC_TYPES:
            raise ValueError(f"sync_type must be one of {ALLOWED_SYNC_TYPES}")
        return value


@router.post("/health-connect/sync")
async def health_connect_sync(body: HealthConnectSyncRequest, authorization: str | None = Header(default=None)):
    user_id, email = _require_user_id(authorization)
    started_at = datetime.now(timezone.utc)

    results: dict[str, dict[str, int]] = {}
    unsupported_types: list[str] = []
    debug_last_error: str | None = None
    total_received = 0
    total_stored = 0
    total_skipped = 0

    for data_type, raw_records in body.records.items():
        if data_type not in HEALTH_CONNECT_TYPES:
            unsupported_types.append(data_type)
            continue

        table_name = health_connect_table_for(data_type)
        conflict_columns = health_connect_conflict_columns(data_type)
        received = len(raw_records)
        stored = 0
        skipped = 0

        for raw_record in raw_records:
            normalized_rows = normalize_health_connect_record(data_type, raw_record)
            if not normalized_rows:
                skipped += 1
                continue
            for row in normalized_rows:
                row["user_id"] = user_id
                row["connection_id"] = None
                try:
                    supabase.table(table_name).upsert(row, on_conflict=conflict_columns).execute()
                    stored += 1
                except Exception as exc:
                    skipped += 1
                    debug_last_error = f"{data_type}: {exc}"[:300]

        results[data_type] = {"received": received, "stored": stored, "skipped": skipped}
        total_received += received
        total_stored += stored
        total_skipped += skipped

    # Same table Google Health's health_sync_service.py already writes to
    # (SYNC_RUN_TABLE) — provider distinguishes the two, connection_id stays
    # null for Health Connect (no OAuth connection exists for it, see
    # migration 041). Best-effort: a logging failure must never turn an
    # otherwise-successful data sync into an error response.
    if total_received > 0 or unsupported_types:
        status = "completed"
        if total_stored == 0 and (total_skipped > 0 or unsupported_types):
            status = "failed"
        elif total_skipped > 0:
            status = "partial"
        try:
            supabase.table(SYNC_RUN_TABLE).insert({
                "user_id": user_id,
                "connection_id": None,
                "provider": "health_connect",
                "sync_type": body.sync_type,
                "requested_data_types": list(body.records.keys()),
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "records_received": total_received,
                # Matches the existing, documented Google Health limitation
                # (health_sync_service.py): upsert doesn't distinguish
                # insert-vs-update, so every stored row counts as "created".
                "records_created": total_stored,
                "records_updated": 0,
                "records_skipped": total_skipped,
                "error_message": debug_last_error,
            }).execute()
        except Exception:
            pass

    # Best-effort: a completed Health Connect sync can be this user's
    # earliest-ever qualifying action for the "first 20 active beta
    # testers" discount program.
    if total_received > 0 and status == "completed":
        try:
            maybe_claim_discount_slot(email, user_id)
        except Exception:
            pass

    return {"results": results, "unsupported_types": unsupported_types, "debug_last_error": debug_last_error}
