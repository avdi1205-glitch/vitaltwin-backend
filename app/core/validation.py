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
