"""CGM + Nutrition -> Twin signal adapter (Twin Core Phase 2).

Converts VitalTwin's OWN already-persisted CGM/nutrition records
(`vt_cgm_readings`/`vt_nutrition_entries`) into Twin-facing signals, reusing
the EXACT SAME daily-aggregation + trend architecture Phase 1 (Google
Health) already established — `services/twin_signal_shared.py` for the
day-bucketing helpers, `services/trends.py::compute_trend` for the
averaging math itself. No second signal-adapter pattern, no new averaging
implementation.

Identity: both tables are keyed by `email` (unlike Google Health's
`user_id`-keyed tables) — callers pass rows already scoped to the
requesting user's own email (`core/auth.py::require_email`), matching every
other Twin Core query in `routers/chat.py`.

Constitution rule 12 / Step 7 (AI safety): this module only ever describes
the user's OWN recorded values and coverage — no medical ranges, no
hypo/hyperglycemia classification, no nutritional targets, no diagnosis. It
computes real numbers; the AI is told (via `twin_context.py`'s labeling and
`twin_conversation.py`'s system prompt) to explain them, never to infer a
medical condition from them.

Missing-data honesty (Step 2/3): a calendar day with zero raw readings/
entries is genuinely ABSENT from `daily_aggregate`'s output — never
counted as zero glucose or zero calories. `coverage_days`/`coverage_ratio`
below make this explicit and readable rather than silently implied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .trends import TrendResult, compute_trend
from .twin_signal_shared import daily_aggregate, distinct_days, latest_value

SOURCE_CGM = "cgm"
SOURCE_NUTRITION = "nutrition"

RECENT_WINDOW_DAYS = 7
BASELINE_WINDOW_DAYS = 28

CGM_TIME_FIELD = "reading_at"
CGM_VALUE_FIELD = "glucose_value"
CGM_UNIT = "mg/dL"
"""Both currently-supported CSV import formats (LibreView, Dexcom Clarity —
see `routers/health.py::_GLUCOSE_COLUMNS`) only ever recognize mg/dL-labeled
columns, so this unit is a real, verified constant, not an assumption."""

NUTRITION_TIME_FIELD = "logged_at"

# Nutrition signal -> which `vt_nutrition_entries` column it sums per day.
# All 4 are genuinely stored numeric fields (see migration 021) — nothing
# invented. Each day's entries are SUMMED (multiple meals -> one daily
# total), never averaged, matching how "daily calories" is normally meant.
NUTRITION_FIELD_CONFIG: dict[str, dict[str, str]] = {
    "energy_intake": {"column": "calories", "unit": "kcal"},
    "protein": {"column": "protein", "unit": "g"},
    "carbohydrates": {"column": "carbs", "unit": "g"},
    "fat": {"column": "fat", "unit": "g"},
}


@dataclass(frozen=True)
class CGMSignal:
    has_data: bool
    reading_count: int
    coverage_days: int
    window_days: int
    coverage_ratio: float | None
    latest_value: float | None
    latest_observed_at: str | None
    trend: TrendResult


@dataclass(frozen=True)
class CGMBaseline:
    available: bool
    recent_average: float | None
    baseline_average: float | None
    recent_data_points: int
    baseline_data_points: int
    message: str


@dataclass(frozen=True)
class NutritionSignal:
    signal: str
    unit: str
    has_data: bool
    entry_count: int
    logged_days: int
    window_days: int
    coverage_ratio: float | None
    trend: TrendResult


@dataclass(frozen=True)
class NutritionCoverage:
    logged_days: int
    window_days: int
    coverage_ratio: float
    entry_count: int


def build_cgm_signal(rows: list[dict[str, object]], *, today: date, window_days: int = RECENT_WINDOW_DAYS) -> CGMSignal:
    """`rows` must already be scoped to a single user's own
    `vt_cgm_readings` (`.eq("email", ...)`) — this function never touches
    the database. Daily MEAN glucose per day (a CGM emits many readings per
    day; averaging them per day first, per Step 2, avoids a
    heavily-monitored day silently outweighing a lightly-monitored one)."""
    daily_rows = daily_aggregate(rows, time_field=CGM_TIME_FIELD, value_field=CGM_VALUE_FIELD, agg="average")
    trend = compute_trend(daily_rows, field="value", window_days=window_days, today=today)
    latest_val, latest_observed_at = latest_value(rows, time_field=CGM_TIME_FIELD, value_field=CGM_VALUE_FIELD)
    coverage_days = distinct_days(rows, time_field=CGM_TIME_FIELD)
    return CGMSignal(
        has_data=len(rows) > 0,
        reading_count=len(rows),
        coverage_days=coverage_days,
        window_days=window_days,
        coverage_ratio=round(min(coverage_days, window_days) / window_days, 2) if window_days else None,
        latest_value=latest_val,
        latest_observed_at=latest_observed_at,
        trend=trend,
    )


def build_cgm_baseline(rows: list[dict[str, object]], *, today: date) -> CGMBaseline:
    """Same non-overlapping 7d-recent / 28d-baseline-ending-7d-ago windowing
    as `services/personal_baseline.py`/`services/google_health_signals.py`
    — reused via `compute_trend`'s own `end_date` param, not reimplemented."""
    daily_rows = daily_aggregate(rows, time_field=CGM_TIME_FIELD, value_field=CGM_VALUE_FIELD, agg="average")
    baseline_end = today - timedelta(days=RECENT_WINDOW_DAYS)
    recent = compute_trend(daily_rows, field="value", window_days=RECENT_WINDOW_DAYS, today=today)
    baseline = compute_trend(daily_rows, field="value", window_days=BASELINE_WINDOW_DAYS, today=today, end_date=baseline_end)

    if recent.average is None or baseline.average is None:
        return CGMBaseline(
            available=False,
            recent_average=None,
            baseline_average=None,
            recent_data_points=recent.data_points,
            baseline_data_points=baseline.data_points,
            message="Noch nicht genügend CGM-Daten für eine persönliche Baseline.",
        )
    return CGMBaseline(
        available=True,
        recent_average=recent.average,
        baseline_average=baseline.average,
        recent_data_points=recent.data_points,
        baseline_data_points=baseline.data_points,
        message="Persönliche CGM-Baseline verfügbar.",
    )


def build_nutrition_signal(
    rows: list[dict[str, object]], *, signal: str, today: date, window_days: int = RECENT_WINDOW_DAYS
) -> NutritionSignal:
    """`rows` must already be scoped to a single user's own
    `vt_nutrition_entries`. Sums same-day entries (multiple logged meals ->
    one daily total) before trending — a day with zero logged entries is
    genuinely absent, never a fabricated 0 kcal/0 g day (Step 3)."""
    config = NUTRITION_FIELD_CONFIG[signal]
    daily_rows = daily_aggregate(rows, time_field=NUTRITION_TIME_FIELD, value_field=config["column"], agg="sum")
    trend = compute_trend(daily_rows, field="value", window_days=window_days, today=today)
    logged_days = distinct_days(rows, time_field=NUTRITION_TIME_FIELD)
    return NutritionSignal(
        signal=signal,
        unit=config["unit"],
        has_data=len(rows) > 0,
        entry_count=len(rows),
        logged_days=logged_days,
        window_days=window_days,
        coverage_ratio=round(min(logged_days, window_days) / window_days, 2) if window_days else None,
        trend=trend,
    )


def build_nutrition_baseline(rows: list[dict[str, object]], *, signal: str, today: date, min_coverage_ratio: float = 0.5) -> CGMBaseline:
    """Nutrition baseline is only meaningful when logging coverage is
    actually sufficient (Step 5: "Do NOT treat sporadic meal logging as a
    complete nutritional baseline") — requires at least `min_coverage_ratio`
    of BOTH the recent and baseline windows to have at least one logged
    day, in addition to the usual non-null-average check."""
    config = NUTRITION_FIELD_CONFIG[signal]
    daily_rows = daily_aggregate(rows, time_field=NUTRITION_TIME_FIELD, value_field=config["column"], agg="sum")
    baseline_end = today - timedelta(days=RECENT_WINDOW_DAYS)
    recent = compute_trend(daily_rows, field="value", window_days=RECENT_WINDOW_DAYS, today=today)
    baseline = compute_trend(daily_rows, field="value", window_days=BASELINE_WINDOW_DAYS, today=today, end_date=baseline_end)

    recent_coverage = recent.data_points / RECENT_WINDOW_DAYS
    baseline_coverage = baseline.data_points / BASELINE_WINDOW_DAYS

    if (
        recent.average is None
        or baseline.average is None
        or recent_coverage < min_coverage_ratio
        or baseline_coverage < min_coverage_ratio
    ):
        return CGMBaseline(
            available=False,
            recent_average=None,
            baseline_average=None,
            recent_data_points=recent.data_points,
            baseline_data_points=baseline.data_points,
            message="Deine Ernährungsdaten werden noch zu unregelmäßig erfasst für eine verlässliche persönliche Baseline.",
        )
    return CGMBaseline(
        available=True,
        recent_average=recent.average,
        baseline_average=baseline.average,
        recent_data_points=recent.data_points,
        baseline_data_points=baseline.data_points,
        message="Persönliche Ernährungs-Baseline verfügbar.",
    )


def cgm_to_context_dict(signal: CGMSignal) -> dict[str, object]:
    """Shapes a `CGMSignal` into the plain-dict form `twin_context.py`
    consumes — mirrors `google_health_signals.py::signal_to_context_dict`'s
    convention (never a dataclass leaking into the context builder)."""
    return {
        "average": signal.trend.average,
        "data_points": signal.trend.data_points,
        "data_quality": signal.trend.data_quality,
        "latest_value": signal.latest_value,
        "latest_observed_at": signal.latest_observed_at,
        "unit": CGM_UNIT,
        "has_data": signal.has_data,
        "reading_count": signal.reading_count,
        "coverage_days": signal.coverage_days,
        "window_days": signal.window_days,
        "source": SOURCE_CGM if signal.has_data else "none",
    }


def nutrition_to_context_dict(signal: NutritionSignal) -> dict[str, object]:
    return {
        "average": signal.trend.average,
        "data_points": signal.trend.data_points,
        "data_quality": signal.trend.data_quality,
        "unit": signal.unit,
        "has_data": signal.has_data,
        "entry_count": signal.entry_count,
        "logged_days": signal.logged_days,
        "window_days": signal.window_days,
        "source": SOURCE_NUTRITION if signal.has_data else "none",
    }
