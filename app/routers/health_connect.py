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

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core.auth import require_user
from ..core.health_normalization_service import (
    HEALTH_CONNECT_TYPES,
    health_connect_conflict_columns,
    health_connect_table_for,
    normalize_health_connect_record,
)
from ..core.plan_service import has_feature
from ..core.supabase import supabase

router = APIRouter()


def _require_user_id(authorization: str | None) -> int:
    """Mirrors `routers/google_health.py::_require_user_id` exactly — same
    identity resolution, same entitlement (Health Connect and Google Health
    are the same Premium "automatic health data" capability)."""
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")
    if not has_feature(current.email, "google_health"):
        raise HTTPException(
            status_code=403,
            detail="Automatische Gesundheitsdaten sind ein Premium-Feature. Aktiviere Premium unter /preise.",
        )
    return current.user_id


class HealthConnectSyncRequest(BaseModel):
    # data_type -> list of raw plugin records (shape varies per data type,
    # normalize_health_connect_record() validates/extracts defensively).
    records: dict[str, list[dict[str, object]]] = {}


@router.post("/health-connect/sync")
async def health_connect_sync(body: HealthConnectSyncRequest, authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)

    results: dict[str, dict[str, int]] = {}
    unsupported_types: list[str] = []
    debug_last_error: str | None = None

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

    return {"results": results, "unsupported_types": unsupported_types, "debug_last_error": debug_last_error}
