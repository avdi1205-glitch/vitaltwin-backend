"""Monthly Progress — Grundlage (foundation only).

Twin Intelligence Core — Etappe 6 §5.

Etappe 6 explicitly only asks to "prepare" this area, not build the full
Monthly Progress Loop (Constitution Loop Nr. 13) — so this module has no
own database table (see `migrations/007_daily_planning_reflection_loops.sql`
header) and is computed fresh from already-existing data every time it's
requested. The area stays "locked" (`available=False`) until there is
enough real data to say anything at all — never a guessed monthly summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .trends import compute_trend

MIN_CHECKIN_DAYS_FOR_MONTHLY = 10
LOOKBACK_DAYS = 30

TREND_FIELDS = ("sleep_hours", "energy", "movement_minutes", "stress", "mood")

PREFERENCE_MEMORY_TYPES = {"bestaetigte_praeferenz", "abgelehnter_empfehlungstyp"}


@dataclass(frozen=True)
class MonthlyProgressResult:
    available: bool
    data_points: int
    reason: str | None = None
    thirty_day_trends: dict[str, dict[str, object]] = field(default_factory=dict)
    goal_development: list[str] = field(default_factory=list)
    habit_summary: list[str] = field(default_factory=list)
    changed_preferences: list[str] = field(default_factory=list)
    confirmed_patterns: list[str] = field(default_factory=list)
    memory_development: dict[str, int] = field(default_factory=dict)
    next_month_goal_suggestions: list[str] = field(default_factory=list)


def prepare_monthly_progress(
    *,
    daily_entries: list[dict[str, object]],
    habits: list[dict[str, object]],
    goals: list[dict[str, object]],
    confirmed_memories: list[dict[str, object]],
    confirmed_patterns: list[dict[str, object]],
    today: date,
) -> MonthlyProgressResult:
    data_points = len({e.get("entry_date") for e in daily_entries if e.get("entry_date")})
    if data_points < MIN_CHECKIN_DAYS_FOR_MONTHLY:
        return MonthlyProgressResult(
            available=False,
            data_points=data_points,
            reason=(
                f"Für eine Monatsübersicht werden mindestens {MIN_CHECKIN_DAYS_FOR_MONTHLY} Tage mit Daten "
                f"in den letzten {LOOKBACK_DAYS} Tagen benötigt (aktuell {data_points})."
            ),
        )

    trends: dict[str, dict[str, object]] = {}
    for field_name in TREND_FIELDS:
        result = compute_trend(daily_entries, field=field_name, window_days=LOOKBACK_DAYS, today=today)
        trends[field_name] = {
            "average": result.average,
            "data_points": result.data_points,
            "data_quality": result.data_quality,
        }

    goal_development = [
        f'"{g.get("title") or "Ziel"}" ({g.get("status")})' for g in goals if g.get("status") in ("active", "completed")
    ]
    habit_summary = [
        f'"{h.get("name") or "Gewohnheit"}": {round(float(h["completion_rate_30d"]) * 100)}%'
        for h in habits
        if isinstance(h.get("completion_rate_30d"), (int, float))
    ]
    changed_preferences = [
        str(m.get("human_readable_value"))
        for m in confirmed_memories
        if m.get("memory_type") in PREFERENCE_MEMORY_TYPES and m.get("human_readable_value")
    ]
    pattern_notes = [
        str(p.get("summary")) for p in confirmed_patterns if p.get("status") == "active" and p.get("summary")
    ]
    memory_development: dict[str, int] = {}
    for memory in confirmed_memories:
        status = str(memory.get("status") or "unknown")
        memory_development[status] = memory_development.get(status, 0) + 1

    suggestions = []
    if any(g.get("status") == "active" for g in goals):
        suggestions.append("Bestehende aktive Ziele weiterverfolgen und bei Bedarf anpassen.")
    if not goals:
        suggestions.append("Vielleicht ist jetzt ein guter Zeitpunkt für ein erstes konkretes Ziel.")

    return MonthlyProgressResult(
        available=True,
        data_points=data_points,
        thirty_day_trends=trends,
        goal_development=goal_development,
        habit_summary=habit_summary,
        changed_preferences=changed_preferences,
        confirmed_patterns=pattern_notes,
        memory_development=memory_development,
        next_month_goal_suggestions=suggestions,
    )
