"""Longitudinal Twin State comparison (Twin Core Phase 7, Steps 7-8).

Compares two already-built snapshot `state_json` dicts
(`services/twin_state_snapshot.py::build_snapshot_state()` output) and
produces small, deterministic, non-causal, WELLNESS-ONLY explanation
sentences. Reuses `detect_meaningful_changes()` as the only source of truth
for WHAT changed — this module only decides HOW to phrase it for a
customer. Never says "health improved/deteriorated", never a risk/disease
claim — only increased/decreased/more-or-less-consistent/changed/newly
available/no longer supported, per the task's explicit wording rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .twin_state_snapshot import (
    BIOMARKER_UPDATED,
    DATA_QUALITY_CHANGED,
    DOMAIN_ADDED,
    DOMAIN_REMOVED,
    GOAL_HABIT_CHANGED,
    MEMORY_CHANGED,
    PATTERN_CHANGED,
    TREND_CHANGED,
    detect_meaningful_changes,
)

DOMAIN_LABELS: dict[str, str] = {
    "behavioral_wellness": "deine Check-in-Trends",
    "automatic_health": "deine automatisch synchronisierten Gesundheitsdaten",
    "metabolic": "deine Blutzucker-/Ernährungsdaten",
    "biomarker": "deine Biomarker-Zwilling-Berechnung",
    "memory": "was dein Twin über dich gespeichert hat",
    "patterns": "erkannte Muster",
    "goals_habits": "deine Ziele und Gewohnheiten",
}

TREND_FIELD_LABELS: dict[str, str] = {
    "sleep_hours": "Schlafdauer",
    "energy": "Energie",
    "movement_minutes": "Bewegung",
    "stress": "Stress",
    "mood": "Stimmung",
}


@dataclass(frozen=True)
class LongitudinalComparison:
    available: bool
    reason: str | None
    compared_from: str | None
    compared_to: str | None
    changes: list[dict[str, object]] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)


def _describe_change(change: dict[str, object]) -> str | None:
    category = change.get("category")
    if category == DOMAIN_ADDED:
        label = DOMAIN_LABELS.get(str(change.get("domain")), str(change.get("domain")))
        return f"{label.capitalize()} sind seitdem neu verfügbar."
    if category == DOMAIN_REMOVED:
        label = DOMAIN_LABELS.get(str(change.get("domain")), str(change.get("domain")))
        return f"{label.capitalize()} werden derzeit nicht mehr unterstützt."
    if category == TREND_CHANGED:
        field_label = TREND_FIELD_LABELS.get(str(change.get("field")), str(change.get("field")))
        before, after = change.get("before"), change.get("after")
        if before is None:
            return f"{field_label} ist seitdem neu erfasst (Durchschnitt: {after})."
        direction = "gestiegen" if after > before else "gesunken"
        return f"Deine durchschnittliche {field_label} ist von {before} auf {after} {direction}."
    if category == MEMORY_CHANGED:
        return "Dein Twin hat seitdem neue oder weniger langfristige Beobachtungen gespeichert."
    if category == PATTERN_CHANGED:
        return "Die Anzahl der von deinem Twin erkannten Muster hat sich seitdem verändert."
    if category == GOAL_HABIT_CHANGED:
        return "Deine aktiven Ziele oder Gewohnheiten haben sich seitdem verändert."
    if category == BIOMARKER_UPDATED:
        return "Deine Biomarker-Zwilling-Berechnung wurde seitdem aktualisiert."
    if category == DATA_QUALITY_CHANGED:
        return "Wie gut deine Daten insgesamt abgedeckt sind, hat sich seitdem verändert."
    return None


def compare_snapshots(
    older_state: dict[str, object] | None,
    newer_state: dict[str, object],
    *,
    older_created_at: str | None = None,
    newer_created_at: str | None = None,
) -> LongitudinalComparison:
    """Step 7: the customer-facing longitudinal comparison. Only ever
    describes real, already-detected changes — never invents a comparison
    when no earlier snapshot exists."""
    if older_state is None:
        return LongitudinalComparison(
            available=False, reason="Noch keine frühere Aufzeichnung vorhanden, um einen Vergleich zu zeigen.",
            compared_from=None, compared_to=newer_created_at,
        )
    changes = detect_meaningful_changes(older_state, newer_state)
    explanations = [text for text in (_describe_change(c) for c in changes) if text]
    return LongitudinalComparison(
        available=True, reason=None, compared_from=older_created_at, compared_to=newer_created_at,
        changes=changes, explanations=explanations,
    )


def compare_behavioral_baseline(
    older_state: dict[str, object] | None, newer_state: dict[str, object]
) -> dict[str, object]:
    """Step 8: the deterministic historical-comparison FOUNDATION only —
    compares the same behavioral trend averages each snapshot already
    captured; never a new baseline calculation (`services/personal_baseline.py`
    itself stays untouched, this only lets two ALREADY-COMPUTED snapshot
    trend values be compared over a longer horizon)."""
    if older_state is None:
        return {"available": False, "reason": "Noch keine frühere Aufzeichnung vorhanden.", "fields": {}}

    older_domain = (older_state.get("domains") or {}).get("behavioral_wellness") or {}
    newer_domain = (newer_state.get("domains") or {}).get("behavioral_wellness") or {}
    older_trends = (older_domain.get("values") or {}).get("trends", {})
    newer_trends = (newer_domain.get("values") or {}).get("trends", {})

    fields: dict[str, dict[str, object]] = {}
    for field_name, newer_trend in newer_trends.items():
        older_average = (older_trends.get(field_name) or {}).get("average")
        newer_average = newer_trend.get("average")
        delta = None
        if isinstance(older_average, (int, float)) and isinstance(newer_average, (int, float)):
            delta = round(newer_average - older_average, 2)
        fields[field_name] = {"earlier_average": older_average, "current_average": newer_average, "delta": delta}

    return {
        "available": bool(fields),
        "reason": None if fields else "Keine vergleichbaren Trend-Daten vorhanden.",
        "fields": fields,
        "earlier_data_quality": older_domain.get("data_quality"),
        "current_data_quality": newer_domain.get("data_quality"),
    }
