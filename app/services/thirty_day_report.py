"""30-Day Wellness Report ("Erweiterte Berichte", Pro/Family only).

Assembles ALREADY-COMPUTED results from the existing Monthly Progress
foundation (`services/monthly_progress.py`) and Personal Baseline Engine
(`services/personal_baseline.py`) into one report — deliberately does NOT
duplicate their trend/average math (Constitution rule 17: no duplicate
systems). The only new calculation here is "strongest positive/negative
trend", which reuses the SAME `compute_trend` primitive
(`services/trends.py`) and the field labels/direction already defined in
`services/weekly_reflection.py` (imported, not redefined).

Constitution rule 10 / 5: no causality between metrics is ever claimed —
every sentence describes ONE metric's own change over time (e.g. "deine
Schlafdauer stieg"), never "X caused Y". Constitution rule 6: every section
distinguishes measured data from a calculated trend and states data
quality/coverage; never fills a gap with an invented value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import monthly_progress, personal_baseline
from .trends import compute_trend
from .weekly_reflection import METRIC_HIGHER_IS_BETTER, METRIC_LABELS

REPORT_WINDOW_DAYS = 30
HALF_WINDOW_DAYS = REPORT_WINDOW_DAYS // 2
MIN_DAYS_FOR_FULL_REPORT = monthly_progress.MIN_CHECKIN_DAYS_FOR_MONTHLY

# Fields with real, reliably-captured data suitable for a "strongest trend"
# ranking — same set `personal_baseline.py` already trusts for a baseline.
TREND_RANKING_FIELDS = ("sleep_hours", "movement_minutes", "stress")

INSUFFICIENT_DATA_MESSAGE = (
    f"Für einen vollständigen 30-Tage-Bericht werden mindestens {MIN_DAYS_FOR_FULL_REPORT} Tage mit "
    f"Eintragungen innerhalb der letzten {REPORT_WINDOW_DAYS} Tage benötigt."
)

DISCLAIMER = (
    "Dieser Bericht beschreibt ausschließlich Veränderungen deiner eigenen erfassten Werte über Zeit — "
    "keine medizinische Bewertung, keine Diagnose und keine Aussage über Ursache und Wirkung."
)


@dataclass(frozen=True)
class TrendHighlight:
    field: str
    label: str
    first_half_average: float
    second_half_average: float
    signed_delta: float


@dataclass(frozen=True)
class ThirtyDayReport:
    available: bool
    data_points: int
    period_days: int
    reason: str | None
    coverage_ratio: float | None
    trends: dict[str, dict[str, object]]
    baseline_comparison: list[dict[str, object]]
    goal_progress: list[str]
    habit_progress: list[str]
    consistency_patterns: list[str]
    strongest_positive_trend: dict[str, object] | None
    strongest_negative_trend: dict[str, object] | None
    summary: str
    disclaimer: str


def _rank_trend_highlights(entries: list[dict[str, object]], today: date) -> tuple[TrendHighlight | None, TrendHighlight | None]:
    """Compares the most recent 15 days against the preceding 15 days
    (non-overlapping, same bias-avoidance principle as
    `personal_baseline.py`) per field, and returns the single largest real
    improvement and the single largest real decline — never invents a
    highlight for a field missing data on either side."""
    second_half_start = today - timedelta(days=HALF_WINDOW_DAYS - 1)
    first_half_end = second_half_start - timedelta(days=1)

    highlights: list[TrendHighlight] = []
    for metric in TREND_RANKING_FIELDS:
        second_half = compute_trend(entries, field=metric, window_days=HALF_WINDOW_DAYS, today=today)
        first_half = compute_trend(entries, field=metric, window_days=HALF_WINDOW_DAYS, today=today, end_date=first_half_end)
        if second_half.average is None or first_half.average is None:
            continue
        delta = second_half.average - first_half.average
        higher_is_better = METRIC_HIGHER_IS_BETTER.get(metric, True)
        signed_delta = delta if higher_is_better else -delta
        highlights.append(
            TrendHighlight(
                field=metric,
                label=METRIC_LABELS.get(metric, metric),
                first_half_average=first_half.average,
                second_half_average=second_half.average,
                signed_delta=signed_delta,
            )
        )

    if not highlights:
        return None, None
    best = max(highlights, key=lambda h: h.signed_delta)
    worst = min(highlights, key=lambda h: h.signed_delta)
    positive = best if best.signed_delta > 0 else None
    negative = worst if worst.signed_delta < 0 else None
    return positive, negative


def _highlight_to_dict(highlight: TrendHighlight | None) -> dict[str, object] | None:
    if highlight is None:
        return None
    return {
        "field": highlight.field,
        "label": highlight.label,
        "first_half_average": round(highlight.first_half_average, 2),
        "second_half_average": round(highlight.second_half_average, 2),
    }


def build_thirty_day_report(
    *,
    entries: list[dict[str, object]],
    habits: list[dict[str, object]],
    goals: list[dict[str, object]],
    confirmed_memories: list[dict[str, object]],
    confirmed_patterns: list[dict[str, object]],
    today: date,
) -> ThirtyDayReport:
    monthly = monthly_progress.prepare_monthly_progress(
        daily_entries=entries,
        habits=habits,
        goals=goals,
        confirmed_memories=confirmed_memories,
        confirmed_patterns=confirmed_patterns,
        today=today,
    )

    if not monthly.available:
        return ThirtyDayReport(
            available=False,
            data_points=monthly.data_points,
            period_days=REPORT_WINDOW_DAYS,
            reason=monthly.reason or INSUFFICIENT_DATA_MESSAGE,
            coverage_ratio=None,
            trends={},
            baseline_comparison=[],
            goal_progress=[],
            habit_progress=[],
            consistency_patterns=[],
            strongest_positive_trend=None,
            strongest_negative_trend=None,
            summary=monthly.reason or INSUFFICIENT_DATA_MESSAGE,
            disclaimer=DISCLAIMER,
        )

    baseline_report = personal_baseline.build_personal_baseline_report(entries, today)
    positive, negative = _rank_trend_highlights(entries, today)

    summary_parts = [
        f"Dein Bericht basiert auf {monthly.data_points} erfassten Tagen der letzten {REPORT_WINDOW_DAYS} Tage."
    ]
    if positive:
        summary_parts.append(
            f"{positive.label} zeigt im Vergleich der letzten beiden 15-Tage-Hälften die deutlichste positive Entwicklung."
        )
    if negative:
        summary_parts.append(f"{negative.label} zeigt aktuell die deutlichste rückläufige Entwicklung.")
    if not positive and not negative:
        summary_parts.append("Für eine Hervorhebung einzelner Trends reichen die Daten noch nicht aus.")

    return ThirtyDayReport(
        available=True,
        data_points=monthly.data_points,
        period_days=REPORT_WINDOW_DAYS,
        reason=None,
        coverage_ratio=round(monthly.data_points / REPORT_WINDOW_DAYS, 2),
        trends=monthly.thirty_day_trends,
        baseline_comparison=baseline_report.get("items", []),
        goal_progress=monthly.goal_development,
        habit_progress=monthly.habit_summary,
        consistency_patterns=monthly.confirmed_patterns,
        strongest_positive_trend=_highlight_to_dict(positive),
        strongest_negative_trend=_highlight_to_dict(negative),
        summary=" ".join(summary_parts),
        disclaimer=DISCLAIMER,
    )
