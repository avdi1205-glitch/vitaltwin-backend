"""Shared validation building blocks for Twin Intelligence data.

Twin Intelligence Core — Etappe 2 (Validierung).

Centralizes the range/length/date rules so every new router validates
identically instead of re-implementing bounds per endpoint, and so a
future change to a rule (e.g. widening a scale) happens in one place.

Per the VitalTwin Constitution: no silent corrections. Every validator here
either returns a valid value unchanged or raises `ValueError` with a
user-facing German message — callers (Pydantic models) surface that message
back to the user instead of quietly clamping/rounding it.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timezone
from typing import Literal

# 1-10 self-reported wellness scales (energy, mood, stress, motivation,
# sleep quality, recovery — see Etappe 2 spec §6).
SCALE_MIN = 1
SCALE_MAX = 10

MAX_SHORT_TEXT = 200
MAX_LONG_TEXT = 2000

MAX_SLEEP_HOURS = 16.0
MAX_MOVEMENT_MINUTES = 24 * 60

DataSource = Literal[
    "manual",
    "onboarding",
    "check_in",
    "wearable",
    "imported",
    "calculated",
    "ai_generated",
]
"""Where a value came from. Wearables are not connected yet (Etappe 2), but
the value is reserved now so later etappes don't need a migration to add it.
Calculated/AI-generated values must never be stored as if they were directly
measured user input — see `DataQuality` below and Constitution §"Prevention
Loop"/"Recommendation Loop"."""

DataQuality = Literal[
    "missing",
    "partial",
    "user_reported",
    "calculated",
    "imported",
    "verified_source",
    "outdated",
    "conflicting",
]
"""How much a stored value can be trusted. Never invent a value to avoid
`missing`; never report a `calculated` value with `verified_source`-level
confidence."""

# --- Etappe 4: Recommendation / Decision / Outcome / Feedback loops ---------

RecommendationStatus = Literal[
    "proposed",
    "accepted",
    "modified",
    "completed",
    "skipped",
    "rejected",
    "expired",
]

RecommendationPriority = Literal["low", "medium", "high"]

RecommendationSourceType = Literal["rule_based", "ai_generated"]
"""Etappe 4 only implements `rule_based` (§2: "regelbasierte
Beta-Empfehlungen"). `ai_generated` is reserved for a later etappe — never
claim a recommendation came from an AI model it didn't actually come from."""

DecisionType = Literal["accepted", "modified", "skipped", "rejected"]

OutcomeStatus = Literal[
    "not_started",
    "started",
    "partially_completed",
    "completed",
    "not_implemented",
]

OutcomeSource = Literal[
    "user_reported",
    "derived_from_checkin",
    "derived_from_habit_entry",
    "imported_from_wearable",
]
"""Wearable import isn't implemented yet (Etappe 4) — reserved so a later
etappe doesn't need another migration. Never store an outcome without a
real source that produced it (§4: "keine Ergebnisse erfinden")."""

FeedbackHelpfulness = Literal["helpful", "partially_helpful", "not_helpful"]

FeedbackReason = Literal[
    "nicht_passend",
    "falscher_zeitpunkt",
    "zu_schwierig",
    "zu_einfach",
    "bereits_erledigt",
    "unverstaendlich",
    "nicht_relevant",
    "anderer_grund",
]

MAX_FEEDBACK_COMMENT = 500

# --- Etappe 5: Twin Memory, Pattern Detection, Learning Events -------------

MemoryType = Literal[
    "bestaetigte_praeferenz",
    "aktives_langfristiges_ziel",
    "bevorzugte_aktivitaetszeit",
    "erfolgreiche_routine",
    "abgelehnter_empfehlungstyp",
    "bestaetigtes_muster",
    "bevorzugte_kommunikationsform",
    "persoenliche_regel",
]
"""Die acht speicherbaren Memory-Typen aus Etappe 5 §1. `persoenliche_regel`
ist die einzige, die ausschließlich vom Nutzer selbst (nie automatisch vom
Twin) erzeugt werden darf — siehe Etappe 5 §1: "ausdrücklich gespeicherte
persönliche Regel"."""

MemoryStatus = Literal[
    "candidate",
    "active",
    "confirmed",
    "disputed",
    "archived",
    "deleted",
]
"""Lebenszyklus einer Memory (Etappe 5 §5): Beobachtung -> `candidate` ->
wiederholte Bestätigung -> `active` -> Nutzerbestätigung -> `confirmed`.
`disputed` = widersprochen/abgelehnt, aber noch nicht gelöscht. Eine
einmalige Beobachtung darf niemals direkt als `confirmed` gespeichert werden
(§1: "keine absolute Wahrheit aus einer einzelnen Beobachtung")."""

PatternStatus = Literal["active", "discarded"]

LearningEventType = Literal[
    "praeferenz_erkannt",
    "praeferenz_bestaetigt",
    "empfehlung_erfolgreich",
    "empfehlung_abgelehnt",
    "muster_erkannt",
    "muster_verworfen",
    "ziel_angepasst",
    "memory_erstellt",
    "memory_korrigiert",
    "memory_geloescht",
    # Zusätzlich zu den 10 Beispielen aus Etappe 5 §4: eigene Ereignistypen
    # für Memory-Lebenszyklusaktionen (§2/§5), die dort nicht explizit
    # aufgeführt sind, aber dieselbe Dokumentationspflicht haben.
    "memory_bestaetigt",
    "memory_abgelehnt",
    "memory_archiviert",
]

LearningEventSourceType = Literal[
    "recommendation",
    "recommendation_decision",
    "recommendation_outcome",
    "recommendation_feedback",
    "habit",
    "wellness_goal",
    "twin_memory",
    "twin_pattern",
]

MAX_MEMORY_REASON = 280

# --- Etappe 6: Daily Planning, Evening Reflection, Weekly Reflection -------

DailyPlanStatus = Literal["active", "completed", "archived"]

DailyPlanActionStatus = Literal[
    "proposed",
    "accepted",
    "modified",
    "completed",
    "skipped",
    "rejected",
]
"""Mirrors the Recommendation-Loop lifecycle (Etappe 4) for consistency —
a planned action goes through the same "vorgeschlagen -> Entscheidung des
Nutzers -> Umsetzung" shape (Etappe 6 §1: "Übernehmen oder Ablehnen",
"Anpassungsmöglichkeit")."""

DailyPlanActionSource = Literal["goal", "habit", "recommendation", "carried_over", "manual"]

MaturityLevel = Literal[
    "start",
    "lernt_dich_kennen",
    "erkennt_routinen",
    "versteht_praeferenzen",
    "begleitet_langfristig",
]
"""Twin-Reifegrad (Etappe 6 §6) — rein datengestützt, siehe
`services/twin_maturity.py`. Keine der fünf Stufen darf ohne die dort
dokumentierten realen Datenschwellen erreicht werden."""

MAX_REFLECTION_TEXT = 500

# --- Etappe 9: Privacy, Consent, Export, Deletion ---------------------------

ConsentType = Literal[
    "wellness_data_processing",
    "ai_features",
    "chat_storage",
    "wearables_future",
    "marketing",
    "affiliate_tracking",
    "research_optional",
]
"""Getrennte Einwilligungen pro Zweck (Etappe 9 §3) — niemals eine
pauschale Einwilligung für mehrere Zwecke. `wearables_future` ist reserviert
(keine Wearable-Anbindung existiert bislang, siehe Etappe 2/4), damit ein
späteres Feature nicht ohne vorbereitete Einwilligung startet."""

DataCategory = Literal[
    "checkins",
    "habits",
    "habit_entries",
    "goals",
    "daily_plans",
    "reflections",
    "weekly_reflections",
    "recommendations",
    "memories",
    "patterns",
    "chat_history",
    "feedback",
]
"""Löschbare Datenkategorien (Etappe 9 §2) — jede Kategorie kann unabhängig
und vollständig für den anfragenden Nutzer gelöscht werden, ohne die
übrigen Kategorien zu berühren."""

MAX_SYNC_EXPORT_ROWS = 5000
"""Etappe 9 §1 "Große Exporte für spätere Background Jobs vorbereiten":
oberhalb dieser Gesamtzeilenzahl wird der Export synchron abgelehnt (mit
einer ehrlichen Fehlermeldung), statt eine sehr große Antwort zu erzwingen
oder den Prozess zu blockieren — siehe `services/privacy_export.py` und
`docs/TWIN_BETA_LIMITATIONS.md`."""


def validate_scale_1_to_10(value: int | None, *, field_name: str) -> int | None:
    """Energy, mood, stress, motivation, sleep quality, recovery: all 1-10."""
    if value is None:
        return None
    if not (SCALE_MIN <= value <= SCALE_MAX):
        raise ValueError(f"{field_name} muss zwischen {SCALE_MIN} und {SCALE_MAX} liegen.")
    return value


def validate_sleep_hours(value: float | None) -> float | None:
    if value is None:
        return None
    if value < 0 or value > MAX_SLEEP_HOURS:
        raise ValueError(f"Schlafdauer muss zwischen 0 und {MAX_SLEEP_HOURS:.0f} Stunden liegen.")
    return value


def validate_movement_minutes(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError("Bewegungsminuten dürfen nicht negativ sein.")
    if value > MAX_MOVEMENT_MINUTES:
        raise ValueError("Bewegungsminuten dürfen einen vollen Tag (1440 Minuten) nicht überschreiten.")
    return value


def validate_local_date_not_future(value: date | None, *, field_name: str = "Datum") -> date | None:
    """Rejects dates in the future (e.g. a check-in dated tomorrow) — see
    Etappe 2 spec §6 "zukünftige oder unlogische Werte"."""
    if value is None:
        return None
    today = datetime.now(timezone.utc).date()
    if value > today:
        raise ValueError(f"{field_name} darf nicht in der Zukunft liegen.")
    return value


def validate_short_text(
    value: str | None, *, field_name: str, max_length: int = MAX_SHORT_TEXT
) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} darf maximal {max_length} Zeichen lang sein.")
    return stripped


def validate_long_text(value: str | None, *, field_name: str) -> str | None:
    return validate_short_text(value, field_name=field_name, max_length=MAX_LONG_TEXT)


def validate_timezone_name(value: str) -> str:
    """Must be a real IANA timezone (e.g. `Europe/Berlin`) — see Etappe 2
    spec §9. Rejects made-up strings early instead of silently defaulting."""
    try:
        zoneinfo.ZoneInfo(value)
    except Exception as exc:
        raise ValueError("Ungültige Zeitzone (z. B. Europe/Berlin).") from exc
    return value
