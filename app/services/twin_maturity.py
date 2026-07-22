"""Twin-Reifegrad (Twin Maturity Level).

Twin Intelligence Core — Etappe 6 §6.

Purely rule-based over already-computed counts — no "AI-feeling" percentage,
no fake precision (Etappe 6 §6: "Keine scheinwissenschaftliche Genauigkeit").
Every level requires concrete, named data thresholds; the result always
explains which data exists, which is still missing, and exactly what would
move the Twin to the next level — never a vague "getting smarter" claim.

This is entirely optional UI (§6: "Optional darf...") and computed fresh on
every request — no persistence, no own table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.validation import MaturityLevel

MIN_CHECKIN_DAYS_LEARNING = 5
MIN_ACCOUNT_AGE_ROUTINES_DAYS = 14
MIN_ACCOUNT_AGE_PREFERENCES_DAYS = 21
MIN_ACCOUNT_AGE_LONGTERM_DAYS = 60
MIN_CONFIRMED_MEMORIES_PREFERENCES = 2
MIN_CONFIRMED_MEMORIES_LONGTERM = 5
MIN_WEEKLY_REFLECTIONS_LONGTERM = 1

LEVEL_LABELS: dict[MaturityLevel, str] = {
    "start": "Start",
    "lernt_dich_kennen": "Lernt dich kennen",
    "erkennt_routinen": "Erkennt Routinen",
    "versteht_praeferenzen": "Versteht Präferenzen",
    "begleitet_langfristig": "Begleitet dich langfristig",
}

LEVEL_ORDER: list[MaturityLevel] = [
    "start",
    "lernt_dich_kennen",
    "erkennt_routinen",
    "versteht_praeferenzen",
    "begleitet_langfristig",
]


@dataclass(frozen=True)
class MaturityResult:
    level: MaturityLevel
    level_label: str
    present_data: dict[str, object] = field(default_factory=dict)
    missing_data: list[str] = field(default_factory=list)


def _missing_for_next(
    level: MaturityLevel,
    *,
    checkin_day_count: int,
    account_age_days: int,
    confirmed_memory_count: int,
    has_routine_or_time_memory: bool,
    has_confirmed_preference: bool,
    has_active_pattern: bool,
    weekly_reflection_count: int,
) -> list[str]:
    missing: list[str] = []
    if level == "start":
        missing.append(
            f"Noch {max(0, MIN_CHECKIN_DAYS_LEARNING - checkin_day_count)} weitere Check-in-Tage bis zur nächsten Stufe."
        )
    elif level == "lernt_dich_kennen":
        if account_age_days < MIN_ACCOUNT_AGE_ROUTINES_DAYS:
            missing.append(f"Noch {MIN_ACCOUNT_AGE_ROUTINES_DAYS - account_age_days} Tage Nutzungsdauer.")
        if not (has_routine_or_time_memory or has_active_pattern):
            missing.append("Noch keine bestätigte Routine oder erkanntes Muster.")
    elif level == "erkennt_routinen":
        if account_age_days < MIN_ACCOUNT_AGE_PREFERENCES_DAYS:
            missing.append(f"Noch {MIN_ACCOUNT_AGE_PREFERENCES_DAYS - account_age_days} Tage Nutzungsdauer.")
        if not has_confirmed_preference:
            missing.append("Noch keine bestätigte Präferenz.")
        if confirmed_memory_count < MIN_CONFIRMED_MEMORIES_PREFERENCES:
            missing.append(f"Noch {MIN_CONFIRMED_MEMORIES_PREFERENCES - confirmed_memory_count} bestätigte Memory(s).")
    elif level == "versteht_praeferenzen":
        if account_age_days < MIN_ACCOUNT_AGE_LONGTERM_DAYS:
            missing.append(f"Noch {MIN_ACCOUNT_AGE_LONGTERM_DAYS - account_age_days} Tage Nutzungsdauer.")
        if confirmed_memory_count < MIN_CONFIRMED_MEMORIES_LONGTERM:
            missing.append(f"Noch {MIN_CONFIRMED_MEMORIES_LONGTERM - confirmed_memory_count} bestätigte Memory(s).")
        if weekly_reflection_count < MIN_WEEKLY_REFLECTIONS_LONGTERM:
            missing.append("Noch kein vollständiger Wochenrückblick.")
    return missing


def compute_twin_maturity(
    *,
    checkin_day_count: int,
    account_age_days: int,
    confirmed_memory_count: int,
    has_routine_or_time_memory: bool,
    has_confirmed_preference: bool,
    has_active_pattern: bool,
    weekly_reflection_count: int,
) -> MaturityResult:
    level: MaturityLevel = "start"

    if checkin_day_count >= MIN_CHECKIN_DAYS_LEARNING:
        level = "lernt_dich_kennen"

    if (
        level == "lernt_dich_kennen"
        and account_age_days >= MIN_ACCOUNT_AGE_ROUTINES_DAYS
        and (has_routine_or_time_memory or has_active_pattern)
    ):
        level = "erkennt_routinen"

    if (
        level == "erkennt_routinen"
        and account_age_days >= MIN_ACCOUNT_AGE_PREFERENCES_DAYS
        and has_confirmed_preference
        and confirmed_memory_count >= MIN_CONFIRMED_MEMORIES_PREFERENCES
    ):
        level = "versteht_praeferenzen"

    if (
        level == "versteht_praeferenzen"
        and account_age_days >= MIN_ACCOUNT_AGE_LONGTERM_DAYS
        and confirmed_memory_count >= MIN_CONFIRMED_MEMORIES_LONGTERM
        and weekly_reflection_count >= MIN_WEEKLY_REFLECTIONS_LONGTERM
    ):
        level = "begleitet_langfristig"

    missing = _missing_for_next(
        level,
        checkin_day_count=checkin_day_count,
        account_age_days=account_age_days,
        confirmed_memory_count=confirmed_memory_count,
        has_routine_or_time_memory=has_routine_or_time_memory,
        has_confirmed_preference=has_confirmed_preference,
        has_active_pattern=has_active_pattern,
        weekly_reflection_count=weekly_reflection_count,
    )

    return MaturityResult(
        level=level,
        level_label=LEVEL_LABELS[level],
        present_data={
            "checkin_day_count": checkin_day_count,
            "account_age_days": account_age_days,
            "confirmed_memory_count": confirmed_memory_count,
            "has_routine_or_time_memory": has_routine_or_time_memory,
            "has_confirmed_preference": has_confirmed_preference,
            "has_active_pattern": has_active_pattern,
            "weekly_reflection_count": weekly_reflection_count,
        },
        missing_data=missing,
    )
