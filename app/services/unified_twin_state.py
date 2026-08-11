"""Unified Twin State (Twin Core Phase 4).

VitalTwin has two proven but disconnected Twin concepts today: the
behavioral/longitudinal Twin Core (check-ins, trends, baseline, memory,
patterns, goals/habits, Google Health, CGM, Nutrition, cross-domain
patterns) and the biomarker/biological-age Twin (`routers/twin.py`,
`vt_twin_calculations`). This module is the smallest coherent
READ/COMPOSITION layer over both — it recalculates NOTHING itself. Every
domain summary below is built from an ALREADY-COMPUTED input (the exact
same trend/signal/pattern objects every other Twin Core module already
produces) — no new statistics, no new formula, no new database table.

`DomainSummary` is the canonical contract every domain honestly reports
itself through (Step 2): a domain with no real data is `status="missing"`
(never a fabricated value), one with some-but-thin data is
`status="insufficient_data"`, and one with enough real data is
`status="current"`. No universal "staleness" flag is invented — a domain
only ever exposes its own real `last_updated` timestamp (Step 8); nothing
here decides whether that timestamp counts as "stale" for a given domain,
since no single threshold is valid across e.g. daily wellness vs. a
biomarker panel drawn months apart.

This module never touches the database and never resolves user identity —
every caller (e.g. `routers/profile.py`, `routers/chat.py`) is responsible
for fetching rows already scoped to the single authenticated user (via
`email` for behavioral/biomarker tables, via `core/auth.py::get_user_id_by_email`
for Google-Health-`user_id`-keyed tables) — matching every other Twin Core
service's "pure function over already-fetched data" convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .trends import compute_trend

TREND_FIELDS = ("sleep_hours", "energy", "movement_minutes", "stress", "mood")
MIN_BEHAVIORAL_DATA_POINTS_FOR_CURRENT = 3

MISSING = "missing"
INSUFFICIENT_DATA = "insufficient_data"
CURRENT = "current"


@dataclass(frozen=True)
class DomainSummary:
    domain: str
    status: str  # "current" | "insufficient_data" | "missing"
    source: tuple[str, ...]
    period_days: int | None
    data_count: int | None
    data_quality: str | None
    last_updated: str | None
    values: dict[str, object] = field(default_factory=dict)
    explanation: str | None = None


@dataclass(frozen=True)
class UnifiedTwinState:
    identity_context: dict[str, object]
    behavioral_state: DomainSummary
    automatic_health_state: DomainSummary
    metabolic_state: DomainSummary
    biomarker_state: DomainSummary
    memory_state: DomainSummary
    pattern_state: DomainSummary
    goal_habit_state: DomainSummary
    data_quality_summary: dict[str, int]


def summarize_behavioral_state(daily_entries: list[dict[str, object]], *, today: date, window_days: int = 7) -> DomainSummary:
    data_points = len({e.get("entry_date") for e in daily_entries if e.get("entry_date")})
    if data_points == 0:
        return DomainSummary(
            domain="behavioral_wellness", status=MISSING, source=("check_in",), period_days=window_days,
            data_count=0, data_quality=None, last_updated=None,
            explanation="Noch keine Check-in-Daten vorhanden.",
        )

    trends: dict[str, dict[str, object]] = {}
    for field_name in TREND_FIELDS:
        result = compute_trend(daily_entries, field=field_name, window_days=window_days, today=today)
        if result.average is not None:
            trends[field_name] = {"average": result.average, "data_quality": result.data_quality}

    latest_date = max((str(e.get("entry_date")) for e in daily_entries if e.get("entry_date")), default=None)
    status = CURRENT if data_points >= MIN_BEHAVIORAL_DATA_POINTS_FOR_CURRENT else INSUFFICIENT_DATA
    return DomainSummary(
        domain="behavioral_wellness", status=status, source=("check_in",), period_days=window_days,
        data_count=data_points, data_quality="calculated" if status == CURRENT else "partial",
        last_updated=latest_date, values={"trends": trends},
    )


def summarize_automatic_health_state(google_health: dict[str, dict[str, object]] | None) -> DomainSummary:
    """`google_health` is the ALREADY-BUILT plain-dict context
    `services/google_health_signals.py`/`routers/chat.py` produce — this
    function only re-describes it as a `DomainSummary`, it never re-reads
    `health_activity_records`/etc. itself."""
    present = {k: v for k, v in (google_health or {}).items() if v.get("has_data")}
    if not present:
        return DomainSummary(
            domain="automatic_health", status=MISSING, source=("google_health",), period_days=7,
            data_count=0, data_quality=None, last_updated=None,
            explanation="Keine automatisch synchronisierten Gesundheitsdaten vorhanden.",
        )
    last_updated = max((v.get("latest_observed_at") for v in present.values() if v.get("latest_observed_at")), default=None)
    return DomainSummary(
        domain="automatic_health", status=CURRENT, source=("google_health",), period_days=7,
        data_count=sum(int(v.get("data_points") or 0) for v in present.values()), data_quality="calculated",
        last_updated=last_updated,
        values={signal: {"average": v.get("average"), "unit": v.get("unit")} for signal, v in present.items()},
    )


def summarize_metabolic_state(
    cgm: dict[str, object] | None, nutrition: dict[str, dict[str, object]] | None
) -> DomainSummary:
    """`cgm`/`nutrition` are the ALREADY-BUILT plain-dict contexts
    `services/cgm_nutrition_signals.py` produces — reused, not recomputed."""
    sources: list[str] = []
    values: dict[str, object] = {}
    data_count = 0

    if cgm and cgm.get("has_data"):
        sources.append("cgm")
        data_count += int(cgm.get("data_points") or 0)
        values["glucose"] = {"average": cgm.get("average"), "unit": cgm.get("unit")}

    nutrition_present = {k: v for k, v in (nutrition or {}).items() if v.get("has_data")}
    if nutrition_present:
        sources.append("nutrition")
        data_count += sum(int(v.get("data_points") or 0) for v in nutrition_present.values())
        values["nutrition"] = {signal: {"average": v.get("average"), "unit": v.get("unit")} for signal, v in nutrition_present.items()}

    if not sources:
        return DomainSummary(
            domain="metabolic", status=MISSING, source=(), period_days=7, data_count=0, data_quality=None,
            last_updated=None, explanation="Keine CGM- oder Ernährungsdaten vorhanden.",
        )
    return DomainSummary(
        domain="metabolic", status=CURRENT, source=tuple(sources), period_days=7, data_count=data_count,
        data_quality="calculated", last_updated=None, values=values,
    )


def summarize_biomarker_state(calculation_rows: list[dict[str, object]], *, today: date) -> DomainSummary:
    """Reuses the EXISTING `vt_twin_calculations` rows
    (`routers/twin.py::history` already reads this exact table/shape) —
    never recomputes `biologisches_alter`, never invents a missing marker.
    Defensively re-sorts by `created_at` so an out-of-order caller can never
    surface a stale calculation as "latest" (Step 4: historical older
    calculations must not override the latest one). No staleness flag is
    invented here (Step 8) — only the real timestamp is exposed."""
    if not calculation_rows:
        return DomainSummary(
            domain="biomarker", status=MISSING, source=("biomarker_twin",), period_days=None,
            data_count=0, data_quality=None, last_updated=None,
            explanation="Noch keine Twin-Berechnung durchgeführt.",
        )

    latest = max(calculation_rows, key=lambda row: str(row.get("created_at") or ""))
    marker_breakdown = latest.get("marker_breakdown")
    markers_provided = [
        item.get("marker") for item in (marker_breakdown or []) if isinstance(item, dict) and item.get("marker")
    ]
    return DomainSummary(
        domain="biomarker", status=CURRENT, source=("biomarker_twin",), period_days=None,
        data_count=len(calculation_rows), data_quality="calculated", last_updated=latest.get("created_at"),
        values={
            "biologisches_alter": latest.get("biologisches_alter"),
            "differenz": latest.get("differenz"),
            "markers_provided": markers_provided,
            "scenarios": latest.get("scenarios"),
        },
        explanation="Letzte Twin-Berechnung basierend auf deinen eingetragenen Biomarkern.",
    )


def summarize_memory_state(confirmed_memories: list[dict[str, object]]) -> DomainSummary:
    if not confirmed_memories:
        return DomainSummary(
            domain="memory", status=MISSING, source=("twin_memory",), period_days=None, data_count=0,
            data_quality=None, last_updated=None, explanation="Der Twin hat noch nichts Langfristiges gespeichert.",
        )
    notes = [str(m.get("human_readable_value")) for m in confirmed_memories[:3] if m.get("human_readable_value")]
    return DomainSummary(
        domain="memory", status=CURRENT, source=("twin_memory",), period_days=None,
        data_count=len(confirmed_memories), data_quality="calculated", last_updated=None,
        values={"notes": notes},
    )


def summarize_pattern_state(confirmed_patterns: list[dict[str, object]]) -> DomainSummary:
    if not confirmed_patterns:
        return DomainSummary(
            domain="patterns", status=MISSING, source=("pattern_detection",), period_days=None, data_count=0,
            data_quality=None, last_updated=None, explanation="Noch keine bestätigten Muster vorhanden.",
        )
    summaries = [str(p.get("summary")) for p in confirmed_patterns[:3] if p.get("summary")]
    cross_domain_count = sum(
        1 for p in confirmed_patterns if isinstance(p.get("evidence"), dict) and p["evidence"].get("alignment")
    )
    return DomainSummary(
        domain="patterns", status=CURRENT, source=("pattern_detection",), period_days=None,
        data_count=len(confirmed_patterns), data_quality="calculated", last_updated=None,
        values={"summaries": summaries, "cross_domain_count": cross_domain_count},
    )


def summarize_goal_habit_state(goals: list[dict[str, object]], habits: list[dict[str, object]]) -> DomainSummary:
    active_goals = [g for g in goals if g.get("status") == "active"]
    active_habits = [h for h in habits if h.get("status") == "active"]
    if not active_goals and not active_habits:
        return DomainSummary(
            domain="goals_habits", status=MISSING, source=("goals", "habits"), period_days=None, data_count=0,
            data_quality=None, last_updated=None, explanation="Noch keine aktiven Ziele oder Gewohnheiten vorhanden.",
        )
    rates = [float(h["completion_rate_7d"]) for h in active_habits if isinstance(h.get("completion_rate_7d"), (int, float))]
    return DomainSummary(
        domain="goals_habits", status=CURRENT, source=("goals", "habits"), period_days=7,
        data_count=len(active_goals) + len(active_habits), data_quality="calculated", last_updated=None,
        values={
            "active_goal_count": len(active_goals),
            "active_habit_count": len(active_habits),
            "average_habit_completion_7d": round(sum(rates) / len(rates), 2) if rates else None,
        },
    )


def build_unified_twin_state(
    *,
    profile: dict[str, object] | None,
    daily_entries: list[dict[str, object]],
    goals: list[dict[str, object]],
    habits: list[dict[str, object]],
    confirmed_memories: list[dict[str, object]],
    confirmed_patterns: list[dict[str, object]],
    google_health: dict[str, dict[str, object]] | None,
    cgm: dict[str, object] | None,
    nutrition: dict[str, dict[str, object]] | None,
    biomarker_calculations: list[dict[str, object]],
    today: date,
) -> UnifiedTwinState:
    """Composes every domain summary above into ONE deterministic,
    explainable object representing what VitalTwin currently knows about
    the single authenticated user — never a persisted snapshot (Step 11:
    computed fresh on demand from already-persisted systems; no new table,
    no migration)."""
    behavioral = summarize_behavioral_state(daily_entries, today=today)
    automatic_health = summarize_automatic_health_state(google_health)
    metabolic = summarize_metabolic_state(cgm, nutrition)
    biomarker = summarize_biomarker_state(biomarker_calculations, today=today)
    memory = summarize_memory_state(confirmed_memories)
    pattern = summarize_pattern_state(confirmed_patterns)
    goal_habit = summarize_goal_habit_state(goals, habits)

    statuses = [s.status for s in (behavioral, automatic_health, metabolic, biomarker, memory, pattern, goal_habit)]
    data_quality_summary = {status: statuses.count(status) for status in (CURRENT, INSUFFICIENT_DATA, MISSING)}

    return UnifiedTwinState(
        identity_context={"has_profile": profile is not None},
        behavioral_state=behavioral,
        automatic_health_state=automatic_health,
        metabolic_state=metabolic,
        biomarker_state=biomarker,
        memory_state=memory,
        pattern_state=pattern,
        goal_habit_state=goal_habit,
        data_quality_summary=data_quality_summary,
    )
