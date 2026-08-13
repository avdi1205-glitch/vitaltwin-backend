"""Health Connect (Android on-device) — Phase 2: steps ingestion only.

Mounted at `/api/health` in `app/main.py` — the SAME top-level prefix as the
existing `routers/health.py` (CGM/nutrition) and `routers/google_health.py`
(cloud OAuth sync). Confirmed collision-free: this file only defines
`/health-connect/sync`.

Deliberately a THIN endpoint, not a second sync engine: it reuses the exact
same canonical `health_activity_records` table, the exact same
`(user_id, data_type, provider_record_name)` dedupe/on_conflict strategy
already used by `core/health_sync_service.py` for Google Health, and the
exact same entitlement gate (`google_health` — both represent the SAME
Premium "automatic health data" capability, just a different data source).

Health Connect has no OAuth "connection" row (see migration 035) — every
row this endpoint writes has `connection_id = None`.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core.auth import require_user
from ..core.health_normalization_service import normalize_health_connect_steps
from ..core.health_sync_service import _conflict_columns
from ..core.plan_service import has_feature
from ..core.supabase import supabase

router = APIRouter()

ACTIVITY_TABLE = "health_activity_records"


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


class HealthConnectStepsRecord(BaseModel):
    id: str | None = None
    count: float
    startTime: str
    endTime: str | None = None


class HealthConnectSyncRequest(BaseModel):
    records: list[HealthConnectStepsRecord] = []


@router.post("/health-connect/sync")
async def health_connect_sync(body: HealthConnectSyncRequest, authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)

    received = len(body.records)
    stored = 0
    skipped = 0

    for record in body.records:
        normalized = normalize_health_connect_steps(record.model_dump())
        if normalized is None:
            skipped += 1
            continue
        normalized["user_id"] = user_id
        normalized["connection_id"] = None
        try:
            supabase.table(ACTIVITY_TABLE).upsert(
                normalized,
                on_conflict=_conflict_columns("steps"),
            ).execute()
            stored += 1
        except Exception:
            skipped += 1

    return {"received": received, "stored": stored, "skipped": skipped}
