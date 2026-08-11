"""Shared, provider-agnostic date-bucketing utilities for Twin signal
adapters (`services/google_health_signals.py`, `services/cgm_nutrition_signals.py`).

Extracted so CGM/Nutrition (Twin Core Phase 2) reuses the EXACT SAME
daily-aggregation approach Google Health (Phase 1) already uses, instead of
a second, slightly-different implementation. No database access, no
provider-specific knowledge — purely a "list of dicts with a timestamp
field" -> "one row per calendar day" transform, reused by any current or
future signal source.
"""

from __future__ import annotations

from datetime import date, datetime


def to_date(raw: object) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def daily_aggregate(rows: list[dict[str, object]], *, time_field: str, value_field: str, agg: str) -> list[dict[str, object]]:
    """Collapses possibly-multiple-raw-records-per-day into ONE row per
    calendar day (`entry_date` + `value`), shaped exactly like a
    `vt_daily_wellness_entries` row so `services/trends.py::compute_trend`
    can be reused unmodified. Necessary because `compute_trend` weighs every
    row in its window equally — without this pre-aggregation, a day with
    more raw records (e.g. many CGM readings, or several logged meals)
    would silently outweigh a day with fewer.

    A calendar day with ZERO raw rows simply never becomes a key in
    `buckets` — it is genuinely ABSENT from the result, never a fabricated
    zero. This is what makes "missing day != recorded zero" true by
    construction wherever this helper is used."""
    buckets: dict[date, list[float]] = {}
    for row in rows:
        day = to_date(row.get(time_field))
        value = row.get(value_field)
        if day is None or value is None:
            continue
        buckets.setdefault(day, []).append(float(value))
    daily: list[dict[str, object]] = []
    for day, values in buckets.items():
        value = sum(values) if agg == "sum" else sum(values) / len(values)
        daily.append({"entry_date": day.isoformat(), "value": value})
    return daily


def latest_value(rows: list[dict[str, object]], *, time_field: str, value_field: str) -> tuple[float | None, str | None]:
    latest_time: str | None = None
    latest_val: float | None = None
    for row in rows:
        raw_time = row.get(time_field)
        if not raw_time:
            continue
        if latest_time is None or str(raw_time) > latest_time:
            candidate = row.get(value_field)
            if candidate is not None:
                latest_time = str(raw_time)
                latest_val = float(candidate)
    return latest_val, latest_time


def distinct_days(rows: list[dict[str, object]], *, time_field: str) -> int:
    """Number of distinct calendar days with at least one raw row —
    "logging frequency"/"coverage" building block. Never invents a day that
    wasn't actually recorded."""
    days: set[date] = set()
    for row in rows:
        day = to_date(row.get(time_field))
        if day is not None:
            days.add(day)
    return len(days)
