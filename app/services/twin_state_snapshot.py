"""Persistent Longitudinal Twin State — snapshot contract, meaningful-change
detection, and low-noise persistence policy.

Twin Core Phase 7.

A "snapshot" is a small, deterministic, DERIVED description of what
`services/unified_twin_state.py::build_unified_twin_state()` already
computed at one point in time — never a second Twin engine, never a raw-data
backup. This module recalculates NOTHING: every value it serializes is read
straight off the already-composed `UnifiedTwinState`/`DomainSummary`
dataclasses (trend averages, biomarker values, memory/pattern/goal counts —
the exact same small derived numbers those domains already expose). Raw CGM
rows, raw Google Health rows, raw nutrition rows, raw chats, OAuth tokens,
and any other canonical source data are NEVER duplicated here — they remain
solely in their own existing tables.

`SNAPSHOT_VERSION` is an explicit, deliberately-simple compatibility marker
(Step 2) — no migration framework, just an integer stamped onto every stored
snapshot so future code can tell which structure produced an old row.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

from .unified_twin_state import DomainSummary, UnifiedTwinState

SNAPSHOT_VERSION = 1
"""v1: the 7 domains + data_quality_summary shape defined in
`unified_twin_state.py` as of Twin Core Phase 4/7. Any future structural
change to what a snapshot stores must bump this constant — old rows keep
their own recorded `snapshot_version`, they are never rewritten in place."""

DOMAIN_ADDED = "DOMAIN_ADDED"
DOMAIN_REMOVED = "DOMAIN_REMOVED"
TREND_CHANGED = "TREND_CHANGED"
MEMORY_CHANGED = "MEMORY_CHANGED"
PATTERN_CHANGED = "PATTERN_CHANGED"
GOAL_HABIT_CHANGED = "GOAL_HABIT_CHANGED"
BIOMARKER_UPDATED = "BIOMARKER_UPDATED"
DATA_QUALITY_CHANGED = "DATA_QUALITY_CHANGED"
"""Step 4's classification set, minus "BASELINE_CHANGED" — no snapshot field
here is distinct from a behavioral trend average, so that category is only
used by `services/twin_longitudinal_comparison.py`'s longer-horizon
comparisons (Step 8), never invented here just to fill the list."""

MEANINGFUL_TREND_DELTA = 0.3
"""Below this absolute difference, a trend average's change is treated as
noise, not evolution (Step 4: "do not treat tiny floating-point differences
as meaningful"). Same order of magnitude as `pattern_detection.py`'s own
`MEANINGFUL_CORRELATION` threshold — a policy choice, not a scientific one."""

MEANINGFUL_HABIT_COMPLETION_DELTA = 0.1

MAX_ROUTINE_SNAPSHOTS_PER_DAY = 1
"""Step 3: at most one routine snapshot per user per calendar day."""

_MAJOR_CHANGE_CATEGORIES = frozenset({BIOMARKER_UPDATED})
"""Change categories important enough to justify a SECOND snapshot on the
same calendar day (Step 3: "...unless a major explicit state-changing event
justifies an additional checkpoint")."""


def _domain_to_dict(summary: DomainSummary) -> dict[str, object]:
    payload = asdict(summary)
    payload["source"] = list(summary.source)
    return payload


def build_snapshot_state(state: UnifiedTwinState) -> dict[str, object]:
    """The deterministic `state_json` content for one snapshot — every
    field here is a small derived value already present on `state`, never a
    raw source row (Step 1: "not a raw-data backup")."""
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "identity_context": dict(state.identity_context),
        "domains": {
            "behavioral_wellness": _domain_to_dict(state.behavioral_state),
            "automatic_health": _domain_to_dict(state.automatic_health_state),
            "metabolic": _domain_to_dict(state.metabolic_state),
            "biomarker": _domain_to_dict(state.biomarker_state),
            "memory": _domain_to_dict(state.memory_state),
            "patterns": _domain_to_dict(state.pattern_state),
            "goals_habits": _domain_to_dict(state.goal_habit_state),
        },
        "data_quality_summary": dict(state.data_quality_summary),
    }


def has_any_real_domain(snapshot_state: dict[str, object]) -> bool:
    domains = snapshot_state.get("domains")
    if not isinstance(domains, dict):
        return False
    return any(d.get("status") != "missing" for d in domains.values() if isinstance(d, dict))


def detect_meaningful_changes(
    previous_state: dict[str, object] | None, new_state: dict[str, object]
) -> list[dict[str, object]]:
    """Deterministic diff between two `build_snapshot_state()` outputs.
    Returns an empty list if nothing meaningful changed (Step 4) — never
    reports a change for tiny/noisy differences."""
    changes: list[dict[str, object]] = []
    if previous_state is None:
        for domain_name, domain in (new_state.get("domains") or {}).items():
            if isinstance(domain, dict) and domain.get("status") != "missing":
                changes.append({"category": DOMAIN_ADDED, "domain": domain_name})
        return changes

    old_domains = previous_state.get("domains") or {}
    new_domains = new_state.get("domains") or {}

    for domain_name, new_domain in new_domains.items():
        if not isinstance(new_domain, dict):
            continue
        old_domain = old_domains.get(domain_name) if isinstance(old_domains.get(domain_name), dict) else {}
        old_status = old_domain.get("status", "missing")
        new_status = new_domain.get("status", "missing")

        if old_status == "missing" and new_status != "missing":
            changes.append({"category": DOMAIN_ADDED, "domain": domain_name})
            continue
        if old_status != "missing" and new_status == "missing":
            changes.append({"category": DOMAIN_REMOVED, "domain": domain_name})
            continue
        if new_status == "missing":
            continue  # still missing on both sides -> nothing to compare

        if domain_name == "behavioral_wellness":
            old_trends = (old_domain.get("values") or {}).get("trends", {})
            new_trends = (new_domain.get("values") or {}).get("trends", {})
            for field_name, new_trend in new_trends.items():
                old_average = (old_trends.get(field_name) or {}).get("average")
                new_average = new_trend.get("average")
                if isinstance(old_average, (int, float)) and isinstance(new_average, (int, float)):
                    if abs(new_average - old_average) >= MEANINGFUL_TREND_DELTA:
                        changes.append(
                            {"category": TREND_CHANGED, "domain": domain_name, "field": field_name,
                             "before": old_average, "after": new_average}
                        )
                elif new_average is not None and old_average is None:
                    changes.append({"category": TREND_CHANGED, "domain": domain_name, "field": field_name,
                                     "before": None, "after": new_average})
        elif domain_name == "biomarker":
            old_updated = old_domain.get("last_updated")
            new_updated = new_domain.get("last_updated")
            if new_updated and new_updated != old_updated:
                changes.append(
                    {"category": BIOMARKER_UPDATED, "domain": domain_name,
                     "before": old_updated, "after": new_updated}
                )
        elif domain_name == "memory":
            old_count = old_domain.get("data_count")
            new_count = new_domain.get("data_count")
            if old_count != new_count:
                changes.append({"category": MEMORY_CHANGED, "domain": domain_name, "before": old_count, "after": new_count})
        elif domain_name == "patterns":
            old_count = old_domain.get("data_count")
            new_count = new_domain.get("data_count")
            old_cross = (old_domain.get("values") or {}).get("cross_domain_count")
            new_cross = (new_domain.get("values") or {}).get("cross_domain_count")
            if old_count != new_count or old_cross != new_cross:
                changes.append({"category": PATTERN_CHANGED, "domain": domain_name, "before": old_count, "after": new_count})
        elif domain_name == "goals_habits":
            old_values = old_domain.get("values") or {}
            new_values = new_domain.get("values") or {}
            goal_delta = old_values.get("active_goal_count") != new_values.get("active_goal_count")
            habit_delta = old_values.get("active_habit_count") != new_values.get("active_habit_count")
            old_completion = old_values.get("average_habit_completion_7d")
            new_completion = new_values.get("average_habit_completion_7d")
            completion_delta = (
                isinstance(old_completion, (int, float))
                and isinstance(new_completion, (int, float))
                and abs(new_completion - old_completion) >= MEANINGFUL_HABIT_COMPLETION_DELTA
            )
            if goal_delta or habit_delta or completion_delta:
                changes.append({"category": GOAL_HABIT_CHANGED, "domain": domain_name})

    old_quality = previous_state.get("data_quality_summary") or {}
    new_quality = new_state.get("data_quality_summary") or {}
    if old_quality != new_quality:
        changes.append({"category": DATA_QUALITY_CHANGED, "before": old_quality, "after": new_quality})

    return changes


def _snapshot_date(created_at: str | None) -> date | None:
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def decide_snapshot_persistence(
    *, last_snapshot_row: dict[str, object] | None, new_snapshot_state: dict[str, object], today: date
) -> tuple[bool, list[dict[str, object]]]:
    """Step 3 + Step 4 combined: returns `(should_persist, changes)`. Never
    persists an entirely-empty first snapshot (nothing to represent yet),
    never persists a second routine snapshot on the same calendar day unless
    a major change category justifies it, never persists when nothing
    meaningful changed at all."""
    if last_snapshot_row is None:
        if not has_any_real_domain(new_snapshot_state):
            return False, []
        return True, detect_meaningful_changes(None, new_snapshot_state)

    previous_state = last_snapshot_row.get("snapshot")
    previous_state = previous_state if isinstance(previous_state, dict) else None
    changes = detect_meaningful_changes(previous_state, new_snapshot_state)
    if not changes:
        return False, []

    last_date = _snapshot_date(last_snapshot_row.get("created_at"))
    if last_date == today:
        if not any(c.get("category") in _MAJOR_CHANGE_CATEGORIES for c in changes):
            return False, changes

    return True, changes
