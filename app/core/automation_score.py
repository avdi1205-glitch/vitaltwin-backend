"""Automation Engine — Automation Score (VitalTwin Enterprise, Founder
Operating System, Submodule G).

A real, computed automation score — **never a fixed or invented
percentage** (the spec explicitly forbids a hardcoded "90%" display).
Built entirely from actual rows in `vt_automation_runs` (this module's own
execution history) plus manually-resolved rows in the existing
`vt_founder_tasks`/`vt_founder_approvals` tables (the founder's manual
decisions) — no other module's data is duplicated, only counted.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from .automation_registry import AUTOMATION_CATEGORIES
from .concurrency import run_parallel
from .supabase import supabase

RUN_TABLE = "vt_automation_runs"
RULE_TABLE = "vt_automation_rules"
TASK_TABLE = "vt_founder_tasks"
APPROVAL_TABLE = "vt_founder_approvals"

WINDOW_DAYS = 30


def _window_start(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _period_stats(start_iso: str, end_iso: str | None) -> dict:
    def _runs() -> list[dict]:
        query = supabase.table(RUN_TABLE).select("status").gte("created_at", start_iso)
        if end_iso:
            query = query.lt("created_at", end_iso)
        try:
            return query.execute().data or []
        except Exception:
            return []

    def _manual_tasks() -> list[dict]:
        try:
            return (
                supabase.table(TASK_TABLE).select("status,auto_resolved")
                .eq("status", "erledigt").eq("auto_resolved", False).gte("updated_at", start_iso).execute().data or []
            )
        except Exception:
            return []

    def _manual_approvals() -> list[dict]:
        try:
            return (
                supabase.table(APPROVAL_TABLE).select("status")
                .in_("status", ["freigegeben", "abgelehnt"]).gte("updated_at", start_iso).execute().data or []
            )
        except Exception:
            return []

    # The 3 lookups above are fully independent — run them concurrently
    # instead of one after another.
    runs, manual_tasks, manual_approvals = run_parallel(_runs, _manual_tasks, _manual_approvals)

    automated = sum(1 for r in runs if r.get("status") == "erfolgreich")
    failed = sum(1 for r in runs if r.get("status") in ("fehlgeschlagen", "dead_letter"))
    manual = len(manual_tasks) + len(manual_approvals)

    total = automated + manual
    percentage = round(automated / total * 100) if total else None
    return {"automated": automated, "manual": manual, "failed": failed, "total": total, "percentage": percentage}


def compute_automation_score() -> dict:
    current_start = _window_start(WINDOW_DAYS)
    previous_start = _window_start(WINDOW_DAYS * 2)

    def _rules() -> list[dict]:
        try:
            return supabase.table(RULE_TABLE).select("category").execute().data or []
        except Exception:
            return []

    def _manual_tasks_all() -> list[dict]:
        try:
            return (
                supabase.table(TASK_TABLE).select("category").eq("status", "erledigt").eq("auto_resolved", False)
                .gte("updated_at", current_start).execute().data or []
            )
        except Exception:
            return []

    # `current`/`previous` (each internally already parallel, see
    # `_period_stats`) and the 2 category lookups below are all
    # independent of each other — run all 4 branches concurrently.
    current, previous, rules, manual_tasks_all = run_parallel(
        lambda: _period_stats(current_start, None),
        lambda: _period_stats(previous_start, current_start),
        _rules,
        _manual_tasks_all,
    )

    trend = None
    if current["percentage"] is not None and previous["percentage"] is not None:
        trend = current["percentage"] - previous["percentage"]

    rules_by_category = Counter(r.get("category") for r in rules if r.get("category"))
    manual_by_category = Counter(t.get("category") for t in manual_tasks_all if t.get("category"))


    category_breakdown = []
    gaps = []
    for category in sorted(AUTOMATION_CATEGORIES):
        rule_count = rules_by_category.get(category, 0)
        manual_count = manual_by_category.get(category, 0)
        category_breakdown.append({"category": category, "rules": rule_count, "manual_occurrences_30d": manual_count})
        if rule_count == 0 and manual_count >= 3:
            gaps.append({"category": category, "manual_occurrences_30d": manual_count})

    return {
        "overall_percentage": current["percentage"],
        "automated_runs_30d": current["automated"],
        "manual_decisions_30d": current["manual"],
        "failed_runs_30d": current["failed"],
        "trend_vs_previous_30d": trend,
        "category_breakdown": category_breakdown,
        "gaps": gaps,
        "note": (
            "Berechnet aus echten Läufen in vt_automation_runs sowie manuell abgeschlossenen "
            "Aufgaben/Freigaben — kein fester oder erfundener Prozentwert."
            if current["total"] else "Noch keine Prozessdaten vorhanden (weder automatisiert noch manuell erfasst)."
        ),
    }
