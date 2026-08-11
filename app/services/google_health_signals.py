"""Google Health -> Twin signal adapter (Twin Core Phase 1).

Converts VitalTwin's OWN already-normalized, already-persisted Google Health
records (`health_activity_records`/`health_sleep_records`/
`health_metric_records`) into Twin-facing signals, reusing the EXACT SAME
averaging primitive every other Twin Core component already uses
(`services/trends.py::compute_trend`) — never a second averaging
implementation.

Provider independence (Constitution rule 8): this module NEVER calls the
Google API and never imports `core/google_health_client.py`/
`core/health_oauth_service.py` — it only reads VitalTwin's own tables. If a
future provider (Apple Health, Garmin, ...) writes into the same 3 tables
with the same shape, this adapter needs zero changes; `twin_context.py`/
`chat.py` never learn a provider even exists.

Identity: these 3 tables are keyed by `user_id` (bigint, `vt_users.id`),
while the rest of the Twin Core (`vt_daily_wellness_entries` and friends) is
keyed by `email` — callers resolve `user_id` via the EXISTING
`core/auth.py::get_user_id_by_email` (no second identity system is created
here).

No interpolation, no fabricated values: a calendar day with no stored
record is simply absent from the average, exactly like `compute_trend`'s
own contract over `vt_daily_wellness_entries`.

Source precedence (Step 5): only "steps" and "sleep_duration" have a
genuine same-concept manual counterpart in `vt_daily_wellness_entries`
(`steps` / `sleep_hours`) — heart-rate/weight/distance/active-minutes have
no manual VitalTwin field at all, so there is no double-counting risk and
no precedence decision to make for them. `resolve_trend_source()` picks,
at READ TIME only, which source the Twin uses for a given computation —
it never deletes, merges, or overwrites either history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .trends import TrendResult, compute_trend
from .twin_signal_shared import daily_aggregate, latest_value

SOURCE_GOOGLE_HEALTH = "google_health"
SOURCE_MANUAL_CHECKIN = "manual_checkin"
SOURCE_NONE = "none"

RECENT_WINDOW_DAYS = 7
BASELINE_WINDOW_DAYS = 28

# One entry per Twin-facing signal: which table/column holds its value, its
# unit, and whether same-day records should be summed (cumulative activity
# metrics) or averaged (point-in-time samples) before trending — see
# `_daily_aggregate` docstring for why this step is necessary and does not
# duplicate `compute_trend`'s own averaging.
SIGNAL_CONFIG: dict[str, dict[str, object]] = {
    "steps": {"table": "health_activity_records", "data_type": "steps", "time_field": "start_time", "value_field": "value", "agg": "sum", "unit": "Schritte"},
    "distance": {"table": "health_activity_records", "data_type": "distance", "time_field": "start_time", "value_field": "value", "agg": "sum", "unit": "Meter"},
    "active_minutes": {"table": "health_activity_records", "data_type": "active-minutes", "time_field": "start_time", "value_field": "value", "agg": "sum", "unit": "Sekunden"},
    "heart_rate": {"table": "health_metric_records", "data_type": "heart-rate", "time_field": "observed_at", "value_field": "value", "agg": "average", "unit": "bpm"},
    "weight": {"table": "health_metric_records", "data_type": "weight", "time_field": "observed_at", "value_field": "value", "agg": "average", "unit": "kg"},
    "sleep_duration": {"table": "health_sleep_records", "data_type": None, "time_field": "start_time", "value_field": "duration_seconds", "agg": "sum", "unit": "Sekunden"},
}

TABLES_FOR_SIGNALS = {str(config["table"]) for config in SIGNAL_CONFIG.values()}

# signal -> the manual `vt_daily_wellness_entries` field it conceptually
# overlaps with (Step 5). Only these two exist; every other signal above
# has no manual counterpart and therefore no precedence question.
MANUAL_FIELD_FOR_SIGNAL: dict[str, str] = {
    "steps": "steps",
    "sleep_duration": "sleep_hours",
}


@dataclass(frozen=True)
class GoogleHealthSignal:
    signal: str
    unit: str
    has_data: bool
    data_points: int
    latest_value: float | None
    latest_observed_at: str | None
    trend: TrendResult


@dataclass(frozen=True)
class GoogleHealthBaseline:
    signal: str
    available: bool
    recent_average: float | None
    baseline_average: float | None
    recent_data_points: int
    baseline_data_points: int
    message: str


@dataclass(frozen=True)
class ResolvedTrend:
    signal: str
    source: str  # SOURCE_GOOGLE_HEALTH | SOURCE_MANUAL_CHECKIN | SOURCE_NONE
    trend: TrendResult


def build_signal(rows: list[dict[str, object]], *, signal: str, today: date, window_days: int = RECENT_WINDOW_DAYS) -> GoogleHealthSignal:
    """`rows` must already be scoped to a single user (caller's own
    Supabase query, filtered `.eq("user_id", ...)` and `.eq("data_type",
    ...)` where applicable) — this function never touches the database."""
    config = SIGNAL_CONFIG[signal]
    daily_rows = daily_aggregate(
        rows, time_field=str(config["time_field"]), value_field=str(config["value_field"]), agg=str(config["agg"])
    )
    trend = compute_trend(daily_rows, field="value", window_days=window_days, today=today)
    latest_val, latest_observed_at = latest_value(rows, time_field=str(config["time_field"]), value_field=str(config["value_field"]))
    return GoogleHealthSignal(
        signal=signal,
        unit=str(config["unit"]),
        has_data=len(rows) > 0,
        data_points=len(rows),
        latest_value=latest_val,
        latest_observed_at=latest_observed_at,
        trend=trend,
    )


def build_baseline(rows: list[dict[str, object]], *, signal: str, today: date) -> GoogleHealthBaseline:
    """Same non-overlapping recent-vs-baseline window methodology as
    `services/personal_baseline.py::compute_field_baselines` (baseline =
    the 28 days immediately BEFORE the 7-day recent window, never
    overlapping) — reused here via `compute_trend`'s own `end_date` param,
    not reimplemented."""
    config = SIGNAL_CONFIG[signal]
    daily_rows = daily_aggregate(
        rows, time_field=str(config["time_field"]), value_field=str(config["value_field"]), agg=str(config["agg"])
    )
    baseline_end = today - timedelta(days=RECENT_WINDOW_DAYS)
    recent = compute_trend(daily_rows, field="value", window_days=RECENT_WINDOW_DAYS, today=today)
    baseline = compute_trend(daily_rows, field="value", window_days=BASELINE_WINDOW_DAYS, today=today, end_date=baseline_end)

    if recent.average is None or baseline.average is None:
        return GoogleHealthBaseline(
            signal=signal,
            available=False,
            recent_average=None,
            baseline_average=None,
            recent_data_points=recent.data_points,
            baseline_data_points=baseline.data_points,
            message="Noch nicht genügend Google-Health-Daten für eine persönliche Baseline.",
        )
    return GoogleHealthBaseline(
        signal=signal,
        available=True,
        recent_average=recent.average,
        baseline_average=baseline.average,
        recent_data_points=recent.data_points,
        baseline_data_points=baseline.data_points,
        message="Persönliche Google-Health-Baseline verfügbar.",
    )


def resolve_trend_source(
    *,
    signal: str,
    google_rows: list[dict[str, object]],
    manual_entries: list[dict[str, object]],
    today: date,
    window_days: int = RECENT_WINDOW_DAYS,
) -> ResolvedTrend:
    """Step 5 — deterministic, read-time-only precedence: real Google
    Health data (automatic, objective) is preferred when it exists for the
    requested window; manual check-in data is the fallback. Neither source
    is ever modified — this only decides which one a given Twin
    computation reads. Only valid for signals in `MANUAL_FIELD_FOR_SIGNAL`;
    every other signal has no manual counterpart, so callers should use
    `build_signal` directly for those."""
    config = SIGNAL_CONFIG[signal]
    google_daily = daily_aggregate(
        google_rows, time_field=str(config["time_field"]), value_field=str(config["value_field"]), agg=str(config["agg"])
    )
    google_trend = compute_trend(google_daily, field="value", window_days=window_days, today=today)
    if google_trend.data_points > 0:
        return ResolvedTrend(signal=signal, source=SOURCE_GOOGLE_HEALTH, trend=google_trend)

    manual_field = MANUAL_FIELD_FOR_SIGNAL[signal]
    manual_trend = compute_trend(manual_entries, field=manual_field, window_days=window_days, today=today)
    source = SOURCE_MANUAL_CHECKIN if manual_trend.data_points > 0 else SOURCE_NONE
    return ResolvedTrend(signal=signal, source=source, trend=manual_trend)


def signal_to_context_dict(result: GoogleHealthSignal | ResolvedTrend, *, unit: str | None = None) -> dict[str, object]:
    """Shapes a signal/resolved-trend into the plain-dict form
    `services/twin_context.py` consumes (mirrors the existing `trends`
    param's shape exactly — never a dataclass leaking into the context
    builder, keeping it provider-agnostic)."""
    if isinstance(result, GoogleHealthSignal):
        return {
            "average": result.trend.average,
            "data_points": result.trend.data_points,
            "data_quality": result.trend.data_quality,
            "latest_value": result.latest_value,
            "latest_observed_at": result.latest_observed_at,
            "unit": result.unit,
            "has_data": result.has_data,
            "source": SOURCE_GOOGLE_HEALTH if result.has_data else SOURCE_NONE,
        }
    return {
        "average": result.trend.average,
        "data_points": result.trend.data_points,
        "data_quality": result.trend.data_quality,
        "latest_value": None,
        "latest_observed_at": None,
        "unit": unit,
        "has_data": result.trend.data_points > 0,
        "source": result.source,
    }
