"""Cross-domain daily signal view (Twin Core Phase 3).

Pure, read-time-only alignment of already-fetched rows from every
LIVE + REAL-DATA VERIFIED Twin Core source (manual check-ins, Google
Health, CGM, Nutrition) into ONE row per calendar day — the smallest
possible representation needed for `pattern_detection.py`'s EXISTING
correlation method (`_detect_correlation_pattern`/`_pearson`) to compare
signals across sources, without a new statistics engine, a new database
table, or a full time-series schema migration.

Missing data = missing (Step 2/5): a signal is only ever a key in a given
day's dict if that source genuinely has at least one real observation for
that day — never a fabricated 0. This is what lets the existing
`_paired_series` (which already skips any day missing either field) also
correctly enforce "insufficient overlapping days" for cross-domain pairs
with zero new code.
"""

from __future__ import annotations

from datetime import date, timedelta

from .twin_signal_shared import daily_aggregate

CHECKIN_FIELDS = ("sleep_hours", "energy", "movement_minutes", "stress", "mood", "steps")


def build_daily_signals(
    *,
    checkin_entries: list[dict[str, object]],
    google_steps_rows: list[dict[str, object]],
    cgm_rows: list[dict[str, object]],
    nutrition_rows: list[dict[str, object]],
    today: date,
    window_days: int,
) -> dict[date, dict[str, float]]:
    """`google_steps_rows` must already be scoped to the user's own
    `user_id` (Google Health tables); `cgm_rows`/`nutrition_rows` and
    `checkin_entries` must already be scoped to the user's own `email` —
    this function never touches the database and never resolves identity
    itself (matches every other Twin Core signal module's convention)."""
    days: dict[date, dict[str, float]] = {}

    for entry in checkin_entries:
        raw = entry.get("entry_date")
        if not raw:
            continue
        day = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        for field in CHECKIN_FIELDS:
            value = entry.get(field)
            if value is not None:
                days.setdefault(day, {})[field] = float(value)

    # Google Health steps: SUM per day (cumulative activity metric) — same
    # aggregation `google_health_signals.py` already uses for "steps".
    for row in daily_aggregate(google_steps_rows, time_field="start_time", value_field="value", agg="sum"):
        day = date.fromisoformat(str(row["entry_date"]))
        days.setdefault(day, {})["google_steps"] = float(row["value"])  # type: ignore[arg-type]

    # CGM: daily MEAN glucose — same aggregation `cgm_nutrition_signals.py`
    # already uses to avoid overweighting heavily-monitored days.
    for row in daily_aggregate(cgm_rows, time_field="reading_at", value_field="glucose_value", agg="average"):
        day = date.fromisoformat(str(row["entry_date"]))
        days.setdefault(day, {})["glucose_mean"] = float(row["value"])  # type: ignore[arg-type]

    # Nutrition carbohydrates: SUM per day (multiple meals -> one daily
    # total), same convention as `cgm_nutrition_signals.py`.
    for row in daily_aggregate(nutrition_rows, time_field="logged_at", value_field="carbs", agg="sum"):
        day = date.fromisoformat(str(row["entry_date"]))
        days.setdefault(day, {})["nutrition_carbs"] = float(row["value"])  # type: ignore[arg-type]

    window_start = today - timedelta(days=window_days - 1)
    return {day: signals for day, signals in days.items() if window_start <= day <= today}


def to_same_day_rows(daily_signals: dict[date, dict[str, float]]) -> list[dict[str, object]]:
    """One row per day, all that day's known signals as sibling keys —
    lets `pattern_detection.py`'s existing SAME-DAY pairing logic
    (`_paired_series`) be reused completely unmodified."""
    return [{"entry_date": day.isoformat(), **signals} for day, signals in daily_signals.items()]


def to_next_day_shifted_rows(
    daily_signals: dict[date, dict[str, float]], *, day_field: str, next_day_field: str
) -> list[dict[str, object]]:
    """The row for day D carries day D's `day_field` value paired with day
    (D+1)'s `next_day_field` value — already correctly time-shifted so the
    existing SAME-DAY correlation logic can be reused unmodified for a
    genuinely NEXT-DAY relationship (Step 4: never mix same-day/next-day)."""
    rows: list[dict[str, object]] = []
    for day, signals in daily_signals.items():
        if day_field not in signals:
            continue
        next_signals = daily_signals.get(day + timedelta(days=1))
        if not next_signals or next_day_field not in next_signals:
            continue
        rows.append({"entry_date": day.isoformat(), day_field: signals[day_field], next_day_field: next_signals[next_day_field]})
    return rows
