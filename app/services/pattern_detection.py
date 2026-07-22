"""Transparent, rule-based pattern detection.

Twin Intelligence Core — Etappe 5 §3.

Pure functions over already-fetched data — no database access, no ML, no
hidden "AI magic". Every detector computes a simple Pearson correlation (or,
for categorical data, a bucket comparison) over the user's own recent data
and only reports a pattern when there is enough data to say anything at all.

Constitution-mandated wording (Etappe 5 §3): every pattern is phrased as
"In deinen bisherigen Daten zeigt sich möglicherweise …" — never "X verursacht
bei dir Y." `PatternDraft.summary` is built this way; callers must not
rephrase it into a causal claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MIN_PATTERN_DATA_POINTS = 5
LOOKBACK_DAYS = 30

MEANINGFUL_CORRELATION = 0.3
"""Below this absolute Pearson-r, a correlation is considered too weak to
report at all (§3: "keine Muster aus einem einzelnen Ereignis" — extended
here to "keine Muster ohne einen erkennbaren Zusammenhang")."""

CONTRADICTION_CORRELATION = 0.2
"""If the first and second half of the observed period disagree in
direction and both halves individually reach at least this magnitude, the
pattern is marked `contradicting=True` (§3: "widersprüchliche Daten
kennzeichnen")."""


@dataclass(frozen=True)
class PatternDraft:
    pattern_type: str
    pattern_key: str
    variables: tuple[str, str]
    direction: str  # "positiv" | "negativ"
    summary: str
    period_days: int
    data_points: int
    confidence: float
    data_quality: str
    contradicting: bool
    evidence: dict[str, object]


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Plain-Python Pearson correlation coefficient. Returns `None` if there
    isn't enough variance to say anything (e.g. all values identical) —
    never divides by zero, never fabricates a coefficient."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x**0.5 * var_y**0.5)


def _paired_series(
    entries: list[dict[str, object]], field_a: str, field_b: str, *, today: date, days: int
) -> tuple[list[date], list[float], list[float]]:
    window_start = today - timedelta(days=days - 1)
    dates: list[date] = []
    xs: list[float] = []
    ys: list[float] = []
    for entry in entries:
        raw_date = entry.get("entry_date")
        if not raw_date:
            continue
        entry_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        if not (window_start <= entry_date <= today):
            continue
        value_a = entry.get(field_a)
        value_b = entry.get(field_b)
        if value_a is None or value_b is None:
            continue
        dates.append(entry_date)
        xs.append(float(value_a))
        ys.append(float(value_b))
    return dates, xs, ys


def _detect_correlation_pattern(
    entries: list[dict[str, object]],
    *,
    pattern_type: str,
    field_a: str,
    field_b: str,
    label_a: str,
    label_b: str,
    today: date,
) -> PatternDraft | None:
    dates, xs, ys = _paired_series(entries, field_a, field_b, today=today, days=LOOKBACK_DAYS)
    if len(xs) < MIN_PATTERN_DATA_POINTS:
        return None

    r = _pearson(xs, ys)
    if r is None or abs(r) < MEANINGFUL_CORRELATION:
        return None

    # Split chronologically in half and check whether both halves agree on
    # direction — §3 "widersprüchliche Daten kennzeichnen".
    ordered = sorted(zip(dates, xs, ys))
    mid = len(ordered) // 2
    contradicting = False
    if mid >= 2 and len(ordered) - mid >= 2:
        first_half = ordered[:mid]
        second_half = ordered[mid:]
        r_first = _pearson([p[1] for p in first_half], [p[2] for p in first_half])
        r_second = _pearson([p[1] for p in second_half], [p[2] for p in second_half])
        if (
            r_first is not None
            and r_second is not None
            and abs(r_first) >= CONTRADICTION_CORRELATION
            and abs(r_second) >= CONTRADICTION_CORRELATION
            and (r_first > 0) != (r_second > 0)
        ):
            contradicting = True

    direction = "positiv" if r > 0 else "negativ"
    connector = "tendenziell höher" if r > 0 else "tendenziell niedriger"
    summary = (
        f"In deinen bisherigen Daten zeigt sich möglicherweise ein Zusammenhang zwischen "
        f"{label_a} und {label_b}: an Tagen mit mehr {label_a} ist {label_b} {connector}. "
        f"Das ist eine mögliche Verbindung in deinen eigenen Daten, keine Ursache."
    )
    if contradicting:
        summary += " Die Daten sind dabei nicht eindeutig — in einem Teil des Zeitraums zeigte sich das Gegenteil."

    confidence = min(0.85, abs(r)) * (0.6 if contradicting else 1.0)

    return PatternDraft(
        pattern_type=pattern_type,
        pattern_key=pattern_type,
        variables=(field_a, field_b),
        direction=direction,
        summary=summary,
        period_days=LOOKBACK_DAYS,
        data_points=len(xs),
        confidence=round(confidence, 2),
        data_quality="calculated" if len(xs) >= MIN_PATTERN_DATA_POINTS + 2 else "partial",
        contradicting=contradicting,
        evidence={"correlation": round(r, 2), "data_points": len(xs)},
    )


def detect_sleep_energy_pattern(entries: list[dict[str, object]], *, today: date) -> PatternDraft | None:
    return _detect_correlation_pattern(
        entries,
        pattern_type="schlafdauer_energie",
        field_a="sleep_hours",
        field_b="energy",
        label_a="Schlafdauer",
        label_b="Energie",
        today=today,
    )


def detect_movement_mood_pattern(entries: list[dict[str, object]], *, today: date) -> PatternDraft | None:
    return _detect_correlation_pattern(
        entries,
        pattern_type="bewegung_stimmung",
        field_a="movement_minutes",
        field_b="mood",
        label_a="Bewegung",
        label_b="Stimmung",
        today=today,
    )


def detect_stress_sleep_quality_pattern(entries: list[dict[str, object]], *, today: date) -> PatternDraft | None:
    return _detect_correlation_pattern(
        entries,
        pattern_type="stress_schlafqualitaet",
        field_a="stress",
        field_b="sleep_quality",
        label_a="Stress",
        label_b="Schlafqualität",
        today=today,
    )


def detect_weekday_routine_pattern(
    habit_entries: list[dict[str, object]], *, today: date
) -> PatternDraft | None:
    """Wochentag und Routinen: vergleicht die Erfüllungsquote der besten und
    schlechtesten Wochentage über die letzten `LOOKBACK_DAYS` Tage."""
    window_start = today - timedelta(days=LOOKBACK_DAYS - 1)
    completed_by_weekday: dict[int, int] = {i: 0 for i in range(7)}
    total_by_weekday: dict[int, int] = {i: 0 for i in range(7)}

    for entry in habit_entries:
        raw_date = entry.get("entry_date")
        if not raw_date:
            continue
        entry_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        if not (window_start <= entry_date <= today):
            continue
        weekday = entry_date.weekday()
        total_by_weekday[weekday] += 1
        if entry.get("completed"):
            completed_by_weekday[weekday] += 1

    total_points = sum(total_by_weekday.values())
    if total_points < MIN_PATTERN_DATA_POINTS:
        return None

    rates = {
        day: (completed_by_weekday[day] / total_by_weekday[day]) for day in range(7) if total_by_weekday[day] > 0
    }
    if len(rates) < 3:
        return None

    best_day = max(rates, key=lambda d: rates[d])
    worst_day = min(rates, key=lambda d: rates[d])
    gap = rates[best_day] - rates[worst_day]
    if gap < 0.3:
        return None

    weekday_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    summary = (
        f"In deinen bisherigen Daten zeigt sich möglicherweise, dass du Gewohnheiten am "
        f"{weekday_names[best_day]} häufiger umsetzt als am {weekday_names[worst_day]}. "
        "Das ist eine mögliche Verbindung in deinen eigenen Daten, keine feste Regel."
    )

    return PatternDraft(
        pattern_type="wochentag_routine",
        pattern_key="wochentag_routine",
        variables=("weekday", "habit_completed"),
        direction="positiv",
        summary=summary,
        period_days=LOOKBACK_DAYS,
        data_points=total_points,
        confidence=round(min(0.8, gap), 2),
        data_quality="calculated" if total_points >= MIN_PATTERN_DATA_POINTS + 3 else "partial",
        contradicting=False,
        evidence={"best_day": weekday_names[best_day], "worst_day": weekday_names[worst_day], "gap": round(gap, 2)},
    )


def detect_recommendation_success_pattern(
    recommendation_history: list[dict[str, object]],
) -> PatternDraft | None:
    """Empfehlungstyp und Erfolgsquote: vergleicht Kategorien mit
    überdurchschnittlich hoher/niedriger Annahmequote."""
    totals: dict[str, int] = {}
    successes: dict[str, int] = {}
    for rec in recommendation_history:
        category = rec.get("category")
        status = rec.get("status")
        if not category or status not in ("accepted", "modified", "completed", "rejected", "skipped"):
            continue
        category = str(category)
        totals[category] = totals.get(category, 0) + 1
        if status in ("accepted", "modified", "completed"):
            successes[category] = successes.get(category, 0) + 1

    total_points = sum(totals.values())
    if total_points < MIN_PATTERN_DATA_POINTS:
        return None

    rates = {cat: successes.get(cat, 0) / count for cat, count in totals.items() if count >= 2}
    if len(rates) < 2:
        return None

    best_category = max(rates, key=lambda c: rates[c])
    worst_category = min(rates, key=lambda c: rates[c])
    gap = rates[best_category] - rates[worst_category]
    if gap < 0.3:
        return None

    summary = (
        f"In deinen bisherigen Daten zeigt sich möglicherweise, dass Empfehlungen zu "
        f'"{best_category}" häufiger angenommen werden als Empfehlungen zu "{worst_category}". '
        "Das ist eine mögliche Verbindung in deinen eigenen Daten, keine feste Regel."
    )

    return PatternDraft(
        pattern_type="empfehlungstyp_erfolgsquote",
        pattern_key="empfehlungstyp_erfolgsquote",
        variables=("category", "decision"),
        direction="positiv",
        summary=summary,
        period_days=0,
        data_points=total_points,
        confidence=round(min(0.8, gap), 2),
        data_quality="calculated" if total_points >= MIN_PATTERN_DATA_POINTS + 3 else "partial",
        contradicting=False,
        evidence={"best_category": best_category, "worst_category": worst_category, "gap": round(gap, 2)},
    )


def generate_patterns(
    *,
    daily_entries: list[dict[str, object]],
    habit_entries: list[dict[str, object]],
    recommendation_history: list[dict[str, object]],
    today: date,
) -> list[PatternDraft]:
    """Runs every detector and collects all resulting drafts. Callers
    (`routers/twin_memory.py`) de-duplicate against existing patterns
    (`pattern_key`) before persisting anything."""
    drafts: list[PatternDraft] = []
    for detector in (detect_sleep_energy_pattern, detect_movement_mood_pattern, detect_stress_sleep_quality_pattern):
        draft = detector(daily_entries, today=today)
        if draft is not None:
            drafts.append(draft)

    weekday_draft = detect_weekday_routine_pattern(habit_entries, today=today)
    if weekday_draft is not None:
        drafts.append(weekday_draft)

    recommendation_draft = detect_recommendation_success_pattern(recommendation_history)
    if recommendation_draft is not None:
        drafts.append(recommendation_draft)

    return drafts
