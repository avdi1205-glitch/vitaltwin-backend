"""Google Health data-type normalization — maps each supported Google Health
`data_type` identifier to its VitalTwin category/table, required OAuth
scope, and standardized internal unit.

Priority order (per spec): Steps -> Sleep -> Heart Rate -> Weight ->
Distance/Active Minutes -> Nutrition (deferred, not implemented yet).

Category -> table:
  "activity" -> health_activity_records (interval-shaped: steps, distance, active-minutes)
  "sleep"    -> health_sleep_records     (session-shaped: sleep stages)
  "metric"   -> health_metric_records    (sample-shaped: heart-rate, weight)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

Category = Literal["activity", "sleep", "metric"]

SCOPE_ACTIVITY = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
SCOPE_SLEEP = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
SCOPE_METRICS = "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"


class DataTypeConfig:
    def __init__(self, *, data_type: str, category: Category, unit: str | None, required_scope: str):
        self.data_type = data_type
        self.category = category
        self.unit = unit
        self.required_scope = required_scope


DATA_TYPE_CONFIG: dict[str, DataTypeConfig] = {
    "steps": DataTypeConfig(data_type="steps", category="activity", unit="count", required_scope=SCOPE_ACTIVITY),
    "sleep": DataTypeConfig(data_type="sleep", category="sleep", unit=None, required_scope=SCOPE_SLEEP),
    "heart-rate": DataTypeConfig(data_type="heart-rate", category="metric", unit="bpm", required_scope=SCOPE_METRICS),
    "weight": DataTypeConfig(data_type="weight", category="metric", unit="kg", required_scope=SCOPE_METRICS),
    "distance": DataTypeConfig(data_type="distance", category="activity", unit="meter", required_scope=SCOPE_ACTIVITY),
    "active-minutes": DataTypeConfig(
        data_type="active-minutes", category="activity", unit="seconds", required_scope=SCOPE_ACTIVITY
    ),
}

# Sync priority order, per spec section "Erste Datentypen".
SYNC_PRIORITY_ORDER = ("steps", "sleep", "heart-rate", "weight", "distance", "active-minutes")


def has_required_scope(data_type: str, granted_scopes: list[str] | tuple[str, ...]) -> bool:
    config = DATA_TYPE_CONFIG.get(data_type)
    if not config:
        return False
    return config.required_scope in granted_scopes


def _parse_dt(value: object) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        # Validate it parses, but store the original ISO string as-is —
        # Postgres timestamptz will normalize it on insert.
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def _camel_case(data_type: str) -> str:
    """`heart-rate` -> `heartRate` — Google nests each data type's raw
    measurement under a member named after its own camelCase identifier."""
    first, *rest = data_type.split("-")
    return first + "".join(part.capitalize() for part in rest)


def normalize_data_point(data_type: str, item: dict[str, object]) -> dict[str, object] | None:
    """Converts one raw Google Health API data point into a row ready for
    the appropriate normalized table. Returns None if the item can't be
    meaningfully normalized (caller should skip it, never crash the sync).
    Always keeps `raw_metadata` populated with the full original item so no
    information is lost even when a specific field's shape differs from
    what was expected."""
    config = DATA_TYPE_CONFIG.get(data_type)
    if not config:
        return None

    interval = item.get("interval") if isinstance(item.get("interval"), dict) else {}
    sample_time = item.get("sampleTime") if isinstance(item.get("sampleTime"), dict) else {}
    session = item.get("session") if isinstance(item.get("session"), dict) else {}
    value_block = item.get("value") if isinstance(item.get("value"), dict) else {}

    provider_record_name = item.get("name") if isinstance(item.get("name"), str) else None
    source_name = None
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    if isinstance(origin, dict):
        source_name = origin.get("originId") or origin.get("packageName")  # type: ignore[assignment]

    raw_value = None
    for key in ("floatValue", "intValue", "value", "count"):
        candidate = value_block.get(key) if value_block else item.get(key)
        if isinstance(candidate, (int, float)):
            raw_value = float(candidate)
            break

    now_iso = datetime.now(timezone.utc).isoformat()

    if config.category == "activity":
        start_time = _parse_dt(interval.get("startTime")) or _parse_dt(item.get("startTime"))
        if not start_time:
            return None
        return {
            "provider": "google_health",
            "provider_record_name": provider_record_name,
            "data_type": data_type,
            "start_time": start_time,
            "end_time": _parse_dt(interval.get("endTime")) or _parse_dt(item.get("endTime")),
            "value": raw_value,
            "unit": config.unit,
            "source_name": source_name,
            "raw_metadata": item,
            "observed_at": now_iso,
        }

    if config.category == "sleep":
        start_time = _parse_dt(session.get("startTime")) or _parse_dt(interval.get("startTime")) or _parse_dt(
            item.get("startTime")
        )
        if not start_time:
            return None
        return {
            "provider": "google_health",
            "provider_record_name": provider_record_name,
            "start_time": start_time,
            "end_time": _parse_dt(session.get("endTime")) or _parse_dt(interval.get("endTime")) or _parse_dt(
                item.get("endTime")
            ),
            "duration_seconds": int(raw_value) if raw_value is not None else None,
            "sleep_stage": item.get("sleepStage") if isinstance(item.get("sleepStage"), str) else None,
            "source_name": source_name,
            "raw_metadata": item,
        }

    # "metric" — single point-in-time sample. Google nests the actual
    # measurement under a data-type-named member, e.g.
    # `item["weight"] = {"sampleTime": {"physicalTime": ...}, "weightGrams": ...}`
    # — confirmed against a real weight data point that was previously
    # silently skipped because only generic top-level keys were checked.
    member = item.get(_camel_case(data_type))
    member = member if isinstance(member, dict) else {}
    member_sample_time = member.get("sampleTime") if isinstance(member.get("sampleTime"), dict) else {}

    observed_at = (
        _parse_dt(member_sample_time.get("physicalTime"))
        or _parse_dt(sample_time.get("physicalTime"))
        or _parse_dt(item.get("physicalTime"))
        or _parse_dt(item.get("time"))
    )
    if not observed_at:
        return None

    metric_value = raw_value
    if metric_value is None:
        for key, val in member.items():
            if isinstance(val, (int, float)):
                # Grams member + a kg-unit data type -> exact unit conversion,
                # not an invented value.
                metric_value = val / 1000 if key.endswith("Grams") and config.unit == "kg" else float(val)
                break

    return {
        "provider": "google_health",
        "provider_record_name": provider_record_name,
        "data_type": data_type,
        "observed_at": observed_at,
        "start_time": _parse_dt(interval.get("startTime")),
        "end_time": _parse_dt(interval.get("endTime")),
        "value": metric_value,
        "unit": config.unit,
        "source_name": source_name,
        "raw_metadata": item,
    }


def normalize_health_connect_steps(item: dict[str, object]) -> dict[str, object] | None:
    """Health Connect Phase 2 — maps ONE Android Health Connect `StepsRecord`
    (as returned by `HealthConnectPlugin.readSteps()`: `{id, count, startTime,
    endTime}`) into the EXACT SAME canonical row shape `normalize_data_point`
    already produces for "steps" — same keys, same table, same dedupe
    strategy (Constitution rule 8: one internal shape per provider adapter,
    never a second canonical model). Returns None if the item can't be
    meaningfully normalized (caller skips it, never crashes the sync)."""
    start_time = _parse_dt(item.get("startTime"))
    if not start_time:
        return None
    count = item.get("count")
    if not isinstance(count, (int, float)):
        return None
    record_id = item.get("id") if isinstance(item.get("id"), str) else None
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "provider": "health_connect",
        "provider_record_name": record_id,
        "data_type": "steps",
        "start_time": start_time,
        "end_time": _parse_dt(item.get("endTime")),
        "value": float(count),
        "unit": "count",
        "source_name": "health_connect",
        "raw_metadata": item,
        "observed_at": now_iso,
    }
