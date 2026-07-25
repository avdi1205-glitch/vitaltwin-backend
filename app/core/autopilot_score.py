"""Founder Autopilot — Founder-OS-wide Automation Score & Work-Saved
Estimate (VitalTwin Enterprise, Founder Operating System, Submodule J).

Aggregates the **already-computed** automation scores of Submodule G
(`core/automation_score.py`) and Submodule I
(`core/documentation_score.py`) rather than re-deriving a third
computation — this is the "roll-up" the spec asks for
("pro Submodul A bis I"), not a duplicate scoring engine.
"""

from __future__ import annotations

from . import automation_score as g_score
from . import documentation_score as i_score
from .supabase import supabase

# Minutes assumed per manually-completed recurring item — a documented,
# openly-stated assumption (per spec: "dokumentierte Durchschnittswerte"),
# never presented as exact fact.
ASSUMED_MINUTES_PER_MANUAL_ITEM = 5


def compute_founder_os_automation_score() -> dict:
    g = g_score.compute_automation_score()
    i = i_score.compute_documentation_automation_score()

    try:
        open_tasks = len([t for t in (supabase.table("vt_founder_tasks").select("status").execute().data or []) if t.get("status") in ("neu", "in_bearbeitung", "warten")])
    except Exception:
        open_tasks = None
    try:
        open_approvals = len([a for a in (supabase.table("vt_founder_approvals").select("status").execute().data or []) if a.get("status") in ("neu", "ki_geprueft", "zur_pruefung")])
    except Exception:
        open_approvals = None

    per_submodule = {
        "G_automation_engine": {"automated_30d": g.get("automated_runs_30d"), "manual_30d": g.get("manual_decisions_30d"), "percentage": g.get("overall_percentage")},
        "I_auto_documentation": {"automated": i.get("auto_generated_drafts"), "manual": i.get("manually_reviewed_documents"), "percentage": i.get("automation_percentage")},
    }

    automated_total = (g.get("automated_runs_30d") or 0) + (i.get("auto_generated_drafts") or 0)
    manual_total = (g.get("manual_decisions_30d") or 0) + (i.get("manually_reviewed_documents") or 0)
    combined_total = automated_total + manual_total
    overall_pct = round(automated_total / combined_total * 100) if combined_total else None

    gaps = []
    if g.get("gaps"):
        gaps.extend(g["gaps"])

    next_step = None
    if overall_pct is not None and overall_pct < 90:
        if gaps:
            next_step = f"Größte Lücke: Kategorie '{gaps[0]['category']}' hat noch keine Automatisierungsregel trotz {gaps[0]['manual_occurrences_30d']} manuellen Vorgängen."
        else:
            next_step = "Automatisierungschancen im Automation-Engine-Tab prüfen, um näher an 90% zu kommen."

    return {
        "overall_percentage": overall_pct,
        "trend_vs_previous_30d": g.get("trend_vs_previous_30d"),
        "per_submodule": per_submodule,
        "open_tasks": open_tasks,
        "open_approvals": open_approvals,
        "gaps": gaps,
        "next_step_towards_90_percent": next_step,
        "note": "Zusammengeführt aus core/automation_score.py (Submodul G) und core/documentation_score.py (Submodul I) — kein fester Wert." if combined_total else "Noch keine Prozessdaten vorhanden.",
    }


def compute_work_saved_estimate() -> dict:
    g = g_score.compute_automation_score()
    automated_operations = g.get("automated_runs_30d") or 0
    estimated_minutes_saved = automated_operations * ASSUMED_MINUTES_PER_MANUAL_ITEM

    return {
        "automated_operations_30d": automated_operations,
        "assumed_minutes_per_operation": ASSUMED_MINUTES_PER_MANUAL_ITEM,
        "estimated_minutes_saved_30d": estimated_minutes_saved,
        "estimated_hours_saved_30d": round(estimated_minutes_saved / 60, 1),
        "uncertainty": "hoch",
        "calculation_method": "automated_operations_30d × angenommene Minuten pro Vorgang — keine tatsächliche Zeitmessung vorhanden.",
        "note": "Schätzung, keine exakte Wahrheit — es gibt keine echte Zeiterfassung manueller Vorgänge in dieser Codebase." if automated_operations else "Noch keine automatisierten Vorgänge vorhanden.",
    }
