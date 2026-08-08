"""Personal Baseline Engine (VitalTwin Mehrwert Phase 1).

Compares a user's recent values against their OWN historical baseline —
never against a generic population average (Constitution rule 5: "Deine
eigene Baseline statt allgemeiner Durchschnittswerte").

Deliberately reuses `services/trends.py::compute_trend()` for the actual
window-average math instead of duplicating it (Constitution rule 17: no
duplicate systems) — this module only adds the baseline-vs-recent
comparison and the honest natural-language framing on top.

Only computes baselines for fields VitalTwin reliably captures TODAY
(`sleep_hours`, `steps`, `movement_minutes` from manual check-ins). Bedtime,
heart rate and weight trend are intentionally NOT computed here because no
such data is currently stored anywhere in `vt_daily_wellness_entries` (no
bedtime column; Google Health is not connected yet) — inventing a baseline
for data that doesn't exist would violate the "keine Fake-Daten" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .trends import compute_trend, TrendResult

BASELINE_WINDOWS = (7, 28)
RECENT_WINDOW = 7
BASELINE_WINDOW = 28

BASELINE_FIELDS = ("sleep_hours", "steps", "movement_minutes")

# Fields the product vision (Constitution + this task) mentions but that
# have no real underlying data source yet — kept explicit and honest rather
# than silently omitted, so the frontend can show "noch nicht verfügbar"
# instead of nothing.
NOT_YET_TRACKED_FIELDS = ("typical_bedtime", "heart_rate", "weight_trend")

_FIELD_LABELS = {
    "sleep_hours": "Deine erfasste Schlafdauer",
    "steps": "Deine Schrittzahl",
    "movement_minutes": "Deine Bewegungszeit",
}

# sleep_hours is naturally read as a duration difference in minutes rather
# than a percentage ("23 Minuten kürzer" reads better than "5% weniger").
_MESSAGE_STYLE = {
    "sleep_hours": "duration_minutes",
    "steps": "percent",
    "movement_minutes": "percent",
}


@dataclass(frozen=True)
class FieldBaseline:
    field: str
    recent: TrendResult
    baseline: TrendResult
    last_updated: str | None


def compute_field_baselines(entries: list[dict[str, object]], today: date) -> dict[str, FieldBaseline]:
    """Real window averages per field, no interpretation yet."""
    latest_entry_date = None
    for entry in entries:
        raw_date = entry.get("entry_date")
        if raw_date and (latest_entry_date is None or str(raw_date) > latest_entry_date):
            latest_entry_date = str(raw_date)

    result: dict[str, FieldBaseline] = {}
    # The baseline window deliberately does NOT overlap with the recent
    # window — otherwise the most recent days would dilute their own
    # comparison baseline (self-referential bias). Baseline = the 28 days
    # immediately BEFORE the 7-day recent window.
    baseline_end = today - timedelta(days=RECENT_WINDOW)
    for field in BASELINE_FIELDS:
        recent = compute_trend(entries, field=field, window_days=RECENT_WINDOW, today=today)
        baseline = compute_trend(
            entries, field=field, window_days=BASELINE_WINDOW, today=today, end_date=baseline_end
        )
        result[field] = FieldBaseline(field=field, recent=recent, baseline=baseline, last_updated=latest_entry_date)
    return result


def _format_message(field: str, recent_average: float, baseline_average: float) -> str:
    label = _FIELD_LABELS.get(field, field)
    style = _MESSAGE_STYLE.get(field, "percent")

    if style == "duration_minutes":
        delta_minutes = round((recent_average - baseline_average) * 60)
        if delta_minutes == 0:
            return f"{label} entspricht in den letzten {RECENT_WINDOW} Tagen deinem persönlichen {BASELINE_WINDOW}-Tage-Wert."
        direction = "länger" if delta_minutes > 0 else "kürzer"
        return (
            f"{label} war in den letzten {RECENT_WINDOW} Tagen durchschnittlich {abs(delta_minutes)} Minuten "
            f"{direction} als dein persönlicher {BASELINE_WINDOW}-Tage-Wert."
        )

    if baseline_average == 0:
        return f"{label} liegt diese Woche im Vergleich zu deiner persönlichen {BASELINE_WINDOW}-Tage-Baseline noch ohne verlässlichen Vergleichswert."
    percent = round((recent_average - baseline_average) / baseline_average * 100)
    if percent == 0:
        return f"{label} entspricht diese Woche deiner persönlichen {BASELINE_WINDOW}-Tage-Baseline."
    direction = "über" if percent > 0 else "unter"
    return f"{label} liegt diese Woche {abs(percent)}% {direction} deiner persönlichen {BASELINE_WINDOW}-Tage-Baseline."


def build_personal_baseline_report(entries: list[dict[str, object]], today: date) -> dict[str, object]:
    """One honest, explainable comparison per tracked field — every entry
    states its own data quality and never invents a number. Matches
    Constitution rule 6 (nachvollziehbare Insights): each item carries
    period, data points, data quality and last-updated timestamp."""
    field_baselines = compute_field_baselines(entries, today)

    items: list[dict[str, object]] = []
    for field, fb in field_baselines.items():
        base_entry: dict[str, object] = {
            "field": field,
            "period_days": BASELINE_WINDOW,
            "recent_window_days": RECENT_WINDOW,
            "recent_data_points": fb.recent.data_points,
            "baseline_data_points": fb.baseline.data_points,
            "recent_data_quality": fb.recent.data_quality,
            "baseline_data_quality": fb.baseline.data_quality,
            "last_updated": fb.last_updated,
        }
        if fb.recent.average is None or fb.baseline.average is None or fb.baseline.data_quality == "missing":
            items.append({
                **base_entry,
                "available": False,
                "message": "VitalTwin lernt noch deine persönliche Baseline.",
            })
            continue

        items.append({
            **base_entry,
            "available": True,
            "recent_average": fb.recent.average,
            "baseline_average": fb.baseline.average,
            "message": _format_message(field, fb.recent.average, fb.baseline.average),
        })

    not_yet_tracked = [
        {
            "field": field,
            "available": False,
            "message": "Noch nicht verfügbar — diese Kennzahl wird aktuell nicht erfasst.",
        }
        for field in NOT_YET_TRACKED_FIELDS
    ]

    return {
        "items": items,
        "not_yet_tracked": not_yet_tracked,
        "disclaimer": (
            "Deine persönliche Baseline vergleicht dich ausschließlich mit deinem eigenen Verlauf, nicht mit "
            "anderen Nutzern. Keine Diagnose, keine Kausalität — nur ein transparenter Vergleich deiner eigenen Werte."
        ),
    }
