"""Trend calculations for the Sleep, Movement, and Stress/Recovery loops.

Twin Intelligence Core — Etappe 3.

Pure functions over already-fetched rows (list of dicts, one per
`vt_daily_wellness_entries` row) — no database access here. Deliberately
produces *transparent, structured* averages, not an AI-generated
interpretation: this etappe explicitly excludes "umfangreiche
KI-Empfehlungslogik" (§2) and any diagnosis (§2, §4).

`data_quality` on the returned `TrendResult` reflects how much the average
can be trusted (few data points -> "partial"/"missing"), per
`core/validation.py::DataQuality` and the Constitution's "keine falsche
Genauigkeit" rule — never silently claim confidence a handful of data points
don't support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MIN_POINTS_FOR_FULL_CONFIDENCE = 4


@dataclass(frozen=True)
class TrendResult:
    field: str
    window_days: int
    average: float | None
    data_points: int
    data_quality: str  # "missing" | "partial" | "calculated"


def compute_trend(
    entries: list[dict[str, object]],
    *,
    field: str,
    window_days: int,
    today: date,
) -> TrendResult:
    """Average of `field` across `entries` whose `entry_date` falls within
    the last `window_days` days (inclusive of today). `entries` is expected
    to already be scoped to a single user (server-side, see `routers/profile.py`).
    """
    window_start = today - timedelta(days=window_days - 1)
    values: list[float] = []

    for entry in entries:
        raw_date = entry.get("entry_date")
        if not raw_date:
            continue
        entry_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        if not (window_start <= entry_date <= today):
            continue
        value = entry.get(field)
        if value is None:
            continue
        values.append(float(value))

    if not values:
        return TrendResult(field=field, window_days=window_days, average=None, data_points=0, data_quality="missing")

    average = round(sum(values) / len(values), 2)
    quality = "calculated" if len(values) >= MIN_POINTS_FOR_FULL_CONFIDENCE else "partial"
    return TrendResult(field=field, window_days=window_days, average=average, data_points=len(values), data_quality=quality)
