"""Advanced Twin Overview ("Vollständiger erweiterter digitaler Zwilling"
V1, Pro/Family only).

Pure composition layer — every section below reuses an ALREADY-COMPUTED
real output from an existing service (Constitution rule 17: no duplicate
systems). No new statistics engine, no AI call, no invented values:

- current 7-day trends       -> `services/trends.py::compute_trend` (same
                                 primitive `/api/profile/trends` uses)
- personal baseline           -> `services/personal_baseline.py` (unchanged)
- 30-day development           -> `services/thirty_day_report.py` (unchanged)
- active goals / habit progress -> plain formatting of already-loaded rows
  (same shape `weekly_reflection.py`/`monthly_progress.py` already format)
- lifestyle simulation entry point -> a static pointer to the existing
  `/api/profile/simulate` feature, never a simulation run here

Constitution rule 6: every section states whether it is available and why
not, rather than silently omitting or fabricating a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .personal_baseline import build_personal_baseline_report
from .thirty_day_report import build_thirty_day_report
from .trends import compute_trend
from .unified_twin_state import summarize_biomarker_state

CURRENT_TREND_FIELDS = ("sleep_hours", "movement_minutes", "stress", "energy", "mood")
CURRENT_TREND_WINDOW_DAYS = 7

LOW_DATA_QUALITY_THRESHOLD = 10  # same bar `monthly_progress.py` uses for a full 30-day view

DISCLAIMER = (
    "Diese Übersicht fasst ausschließlich bereits berechnete, echte Werte aus deinen eigenen Daten zusammen "
    "— keine medizinische Bewertung, keine Diagnose und keine Vorhersage."
)

LIFESTYLE_SIMULATION_NOTE = (
    "Du kannst simulieren, wie sich dein eigener Durchschnitt bei einer angenommenen Veränderung entwickeln "
    "würde (siehe „Wellness-Szenarien“ weiter oben auf dieser Seite)."
)


@dataclass(frozen=True)
class AdvancedTwinOverview:
    available: bool
    data_points: int
    data_quality_overview: str
    reason: str | None
    current_trends: dict[str, dict[str, object]]
    personal_baseline: dict[str, object]
    thirty_day_development: dict[str, object]
    active_goals: list[str]
    habit_progress: list[str]
    lifestyle_simulation: dict[str, object]
    twin_status_summary: str
    disclaimer: str
    biomarker: dict[str, object] | None = None


def _active_goal_notes(goals: list[dict[str, object]]) -> list[str]:
    return [f'"{g.get("title") or "Ziel"}" ({g.get("status")})' for g in goals if g.get("status") == "active"]


def _habit_progress_notes(habits: list[dict[str, object]]) -> list[str]:
    notes: list[str] = []
    for habit in habits:
        rate = habit.get("completion_rate_7d")
        if not isinstance(rate, (int, float)):
            continue
        name = habit.get("name") or "Gewohnheit"
        notes.append(f'"{name}": {round(rate * 100)}% (7 Tage)')
    return notes


def _data_quality_label(data_points: int) -> str:
    if data_points == 0:
        return "keine Daten"
    if data_points < LOW_DATA_QUALITY_THRESHOLD:
        return "gering"
    return "ausreichend"


def _biomarker_payload(biomarker_calculations: list[dict[str, object]] | None, *, today: date) -> dict[str, object]:
    # Independent of the check-in data_points gate — a user can have real
    # biomarker calculations with zero check-ins.
    summary = summarize_biomarker_state(biomarker_calculations or [], today=today)
    return {
        "available": summary.status == "current",
        "biologisches_alter": summary.values.get("biologisches_alter"),
        "differenz": summary.values.get("differenz"),
        "markers_provided": summary.values.get("markers_provided", []),
        "last_updated": summary.last_updated,
        "reason": summary.explanation if summary.status != "current" else None,
    }


def build_advanced_twin_overview(
    *,
    entries: list[dict[str, object]],
    habits: list[dict[str, object]],
    goals: list[dict[str, object]],
    confirmed_memories: list[dict[str, object]],
    confirmed_patterns: list[dict[str, object]],
    today: date,
    biomarker_calculations: list[dict[str, object]] | None = None,
) -> AdvancedTwinOverview:
    data_points = len({e.get("entry_date") for e in entries if e.get("entry_date")})
    quality = _data_quality_label(data_points)
    biomarker = _biomarker_payload(biomarker_calculations, today=today)

    if data_points == 0:
        return AdvancedTwinOverview(
            available=False,
            data_points=0,
            data_quality_overview=quality,
            reason="Noch keine erfassten Tage vorhanden.",
            current_trends={},
            personal_baseline={"items": [], "not_yet_tracked": []},
            thirty_day_development={"available": False, "reason": "Noch keine erfassten Tage vorhanden."},
            active_goals=[],
            habit_progress=[],
            lifestyle_simulation={"available": True, "note": LIFESTYLE_SIMULATION_NOTE},
            twin_status_summary="VitalTwin hat noch keine Daten, um deinen erweiterten Zwilling darzustellen.",
            disclaimer=DISCLAIMER,
            biomarker=biomarker,
        )

    current_trends: dict[str, dict[str, object]] = {}
    for field_name in CURRENT_TREND_FIELDS:
        trend = compute_trend(entries, field=field_name, window_days=CURRENT_TREND_WINDOW_DAYS, today=today)
        current_trends[field_name] = {
            "average": trend.average,
            "data_points": trend.data_points,
            "data_quality": trend.data_quality,
        }

    baseline_report = build_personal_baseline_report(entries, today)
    thirty_day = build_thirty_day_report(
        entries=entries,
        habits=habits,
        goals=goals,
        confirmed_memories=confirmed_memories,
        confirmed_patterns=confirmed_patterns,
        today=today,
    )

    if quality == "gering":
        summary = (
            f"VitalTwin kennt dich erst seit {data_points} erfassten Tagen — deine Übersicht wird mit mehr "
            "Daten zuverlässiger."
        )
    else:
        summary = (
            f"VitalTwin hat {data_points} erfasste Tage ausgewertet und zeigt dir unten deine aktuelle "
            "Baseline, Trends und Entwicklung."
        )

    return AdvancedTwinOverview(
        available=True,
        data_points=data_points,
        data_quality_overview=quality,
        reason=None,
        current_trends=current_trends,
        personal_baseline={
            "items": baseline_report.get("items", []),
            "not_yet_tracked": baseline_report.get("not_yet_tracked", []),
        },
        thirty_day_development={
            "available": thirty_day.available,
            "reason": thirty_day.reason,
            "data_points": thirty_day.data_points,
            "strongest_positive_trend": thirty_day.strongest_positive_trend,
            "strongest_negative_trend": thirty_day.strongest_negative_trend,
            "summary": thirty_day.summary,
        },
        active_goals=_active_goal_notes(goals),
        habit_progress=_habit_progress_notes(habits),
        lifestyle_simulation={"available": True, "note": LIFESTYLE_SIMULATION_NOTE},
        twin_status_summary=summary,
        disclaimer=DISCLAIMER,
        biomarker=biomarker,
    )
