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


# --------------------------------------------------------------------------
# Health Connect (on-device Android) — Phase 2.2: full wellness data type set.
#
# Category -> canonical table (SAME 3 tables Google Health already uses —
# Constitution rule 17, never a second/parallel data store):
#   "activity" -> health_activity_records (interval-shaped)
#   "metric"   -> health_metric_records   (instant-sample-shaped)
#   "sleep"    -> health_sleep_records    (session/stage-shaped)
#
# Deliberately does NOT include "active-minutes" here: unlike Google Health
# (which reports it as its own derived metric), Android Health Connect has
# no native "active minutes" record type — only ActiveCaloriesBurnedRecord
# and ExerciseSessionRecord exist on-device. Listing it here would invent a
# data type this provider can never actually produce.
# --------------------------------------------------------------------------

HealthConnectCategory = Literal["activity", "metric", "sleep"]

HEALTH_CONNECT_TYPES: dict[str, HealthConnectCategory] = {
    "steps": "activity",
    "distance": "activity",
    "active-calories": "activity",
    "total-calories": "activity",
    "exercise-session": "activity",
    "heart-rate": "metric",
    "resting-heart-rate": "metric",
    "heart-rate-variability": "metric",
    "oxygen-saturation": "metric",
    "respiratory-rate": "metric",
    "body-temperature": "metric",
    "weight": "metric",
    "sleep-session": "sleep",
}

HEALTH_CONNECT_UNITS: dict[str, str | None] = {
    "steps": "count",
    "distance": "meter",
    "active-calories": "kcal",
    "total-calories": "kcal",
    "exercise-session": "seconds",
    "heart-rate": "bpm",
    "resting-heart-rate": "bpm",
    "heart-rate-variability": "ms",
    "oxygen-saturation": "percent",
    "respiratory-rate": "breaths_per_minute",
    "body-temperature": "celsius",
    "weight": "kg",
}

# Raw JSON field(s) `HealthConnectPlugin.kt` puts the measured value under,
# per data type (first present numeric key wins) — mirrors the plugin's own
# per-record-type JSON shape, documented in HealthConnectPlugin.kt.
_HEALTH_CONNECT_VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "steps": ("count",),
    "distance": ("distanceMeters",),
    "active-calories": ("energyKcal",),
    "total-calories": ("energyKcal",),
    "exercise-session": ("durationSeconds",),
    "heart-rate": ("beatsPerMinute",),
    "resting-heart-rate": ("beatsPerMinute",),
    "heart-rate-variability": ("rmssdMillis",),
    "oxygen-saturation": ("percentage",),
    "respiratory-rate": ("rate",),
    "body-temperature": ("temperatureCelsius",),
    "weight": ("weightKg",),
}


def _hc_numeric_value(item: dict[str, object], value_keys: tuple[str, ...]) -> float | None:
    for key in value_keys:
        candidate = item.get(key)
        if isinstance(candidate, (int, float)):
            return float(candidate)
    return None


def _hc_interval_activity_row(data_type: str, item: dict[str, object]) -> dict[str, object] | None:
    """Interval-shaped (has startTime/endTime): steps, distance, calories,
    exercise sessions."""
    start_time = _parse_dt(item.get("startTime"))
    if not start_time:
        return None
    value = _hc_numeric_value(item, _HEALTH_CONNECT_VALUE_KEYS.get(data_type, ()))
    if value is None:
        return None
    record_id = item.get("id") if isinstance(item.get("id"), str) else None
    return {
        "provider": "health_connect",
        "provider_record_name": record_id,
        "data_type": data_type,
        "start_time": start_time,
        "end_time": _parse_dt(item.get("endTime")),
        "value": value,
        "unit": HEALTH_CONNECT_UNITS.get(data_type),
        "source_name": "health_connect",
        "raw_metadata": item,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _hc_instant_metric_row(data_type: str, item: dict[str, object]) -> dict[str, object] | None:
    """Instant-sample-shaped (single `time`): heart rate, resting HR, HRV,
    SpO2, respiratory rate, body temperature, weight."""
    observed_at = _parse_dt(item.get("time"))
    if not observed_at:
        return None
    value = _hc_numeric_value(item, _HEALTH_CONNECT_VALUE_KEYS.get(data_type, ()))
    if value is None:
        return None
    record_id = item.get("id") if isinstance(item.get("id"), str) else None
    return {
        "provider": "health_connect",
        "provider_record_name": record_id,
        "data_type": data_type,
        "observed_at": observed_at,
        "start_time": None,
        "end_time": None,
        "value": value,
        "unit": HEALTH_CONNECT_UNITS.get(data_type),
        "source_name": "health_connect",
        "raw_metadata": item,
    }


def _hc_duration_seconds(start_iso: str, end_iso: str) -> int | None:
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return int((end - start).total_seconds())
    except ValueError:
        return None


def _hc_sleep_session_rows(item: dict[str, object]) -> list[dict[str, object]]:
    """One row per sleep stage (matches `health_sleep_records`'s existing
    per-stage row shape) — a stage-less session (some devices only report a
    session total, no stage breakdown) produces exactly one row with
    `sleep_stage=None`. Each stage gets a unique `provider_record_name`
    (`<sessionId>:stage:<index>`) so the dedupe index never collides
    multiple stages of the SAME session onto one row."""
    record_id = item.get("id") if isinstance(item.get("id"), str) else None
    stages = item.get("stages") if isinstance(item.get("stages"), list) else []
    rows: list[dict[str, object]] = []
    for idx, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        stage_start = _parse_dt(stage.get("startTime"))
        stage_end = _parse_dt(stage.get("endTime"))
        if not stage_start or not stage_end:
            continue
        rows.append(
            {
                "provider": "health_connect",
                "provider_record_name": f"{record_id}:stage:{idx}" if record_id else None,
                "start_time": stage_start,
                "end_time": stage_end,
                "duration_seconds": _hc_duration_seconds(stage_start, stage_end),
                "sleep_stage": stage.get("stage") if isinstance(stage.get("stage"), str) else None,
                "source_name": "health_connect",
                "raw_metadata": stage,
            }
        )
    if rows:
        return rows

    session_start = _parse_dt(item.get("startTime"))
    session_end = _parse_dt(item.get("endTime"))
    if not session_start or not session_end:
        return []
    return [
        {
            "provider": "health_connect",
            "provider_record_name": record_id,
            "start_time": session_start,
            "end_time": session_end,
            "duration_seconds": _hc_duration_seconds(session_start, session_end),
            "sleep_stage": None,
            "source_name": "health_connect",
            "raw_metadata": item,
        }
    ]


def normalize_health_connect_record(data_type: str, item: dict[str, object]) -> list[dict[str, object]]:
    """Generic Health Connect (on-device) normalizer covering every
    supported data type in `HEALTH_CONNECT_TYPES` — dispatches by category
    to the correct canonical shape. Always returns a LIST (0, 1, or many
    rows — a sleep session with stages produces one row per stage) so one
    malformed/unrecognized item never crashes the whole sync; the caller
    simply upserts whatever rows come back. Unknown `data_type` -> `[]`
    (reported as unsupported by the router, never silently guessed)."""
    category = HEALTH_CONNECT_TYPES.get(data_type)
    if category is None:
        return []
    if category == "sleep":
        return _hc_sleep_session_rows(item)
    if category == "activity":
        row = _hc_interval_activity_row(data_type, item)
    else:
        row = _hc_instant_metric_row(data_type, item)
    return [row] if row else []


def health_connect_table_for(data_type: str) -> str | None:
    """Which canonical table a Health Connect `data_type` upserts into —
    used by the sync router. Returns None for an unsupported/unknown type
    (caller reports it rather than guessing a table)."""
    category = HEALTH_CONNECT_TYPES.get(data_type)
    if category == "activity":
        return "health_activity_records"
    if category == "metric":
        return "health_metric_records"
    if category == "sleep":
        return "health_sleep_records"
    return None


def health_connect_conflict_columns(data_type: str) -> str:
    """Health-Connect-specific dedupe/on_conflict columns — deliberately
    NOT reusing `health_sync_service._conflict_columns` (that one looks up
    Google Health's `DATA_TYPE_CONFIG`, which doesn't contain Health-Connect
    -only types like `resting-heart-rate`/`exercise-session`/`sleep-session`
    and would raise `KeyError`)."""
    if HEALTH_CONNECT_TYPES.get(data_type) == "sleep":
        return "user_id,provider_record_name"
    return "user_id,data_type,provider_record_name"


def normalize_health_connect_steps(item: dict[str, object]) -> dict[str, object] | None:
    """Health Connect Phase 2's original steps-only normalizer — kept as a
    thin backward-compatible wrapper around the generic
    `normalize_health_connect_record` (Phase 2.2) so existing callers/tests
    referencing this exact name/shape keep working unchanged."""
    rows = normalize_health_connect_record("steps", item)
    return rows[0] if rows else None
