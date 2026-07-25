"""Founder Autopilot — Daily Plan & Weekly Review (VitalTwin Enterprise,
Founder Operating System, Submodule J).

Both computed fresh on read (no stored plan/review record — consistent
with Daily Briefing/Executive Summary elsewhere in this codebase),
strictly capped in size per spec ("Keine Liste mit 100 Punkten").
"""

from __future__ import annotations

from . import automation_score as g_score
from . import autopilot_module_health as health_module
from . import autopilot_orchestrator as orchestrator
from . import autopilot_score as combined_score
from . import documentation_score as i_score
from . import executive_risk_opportunity as h_risk_opp
from .supabase import supabase

MAX_ITEMS_PER_SECTION = 3


def compute_daily_plan() -> dict:
    decisions = orchestrator.get_decision_inbox()[:MAX_ITEMS_PER_SECTION]
    try:
        tasks = supabase.table("vt_founder_tasks").select("*").execute().data or []
        important_tasks = sorted(
            [t for t in tasks if t.get("status") in ("neu", "in_bearbeitung", "warten")],
            key=lambda t: {"kritisch": 3, "hoch": 2, "mittel": 1, "niedrig": 0}.get(t.get("priority"), 0),
            reverse=True,
        )[:MAX_ITEMS_PER_SECTION]
    except Exception:
        important_tasks = []

    risks = h_risk_opp.list_executive_risks()[:MAX_ITEMS_PER_SECTION]
    opportunities = h_risk_opp.list_executive_opportunities()[:MAX_ITEMS_PER_SECTION]

    today_view = orchestrator.get_today_view()

    return {
        "critical_decisions": decisions,
        "important_tasks": important_tasks,
        "opportunities": opportunities,
        "risks": risks,
        "auto_completed_today": today_view.get("auto_completed_today"),
        "waiting_approvals": today_view.get("waiting_approvals"),
        "expected_system_activity_note": "Nächste geplante Läufe siehe Automation-Engine-Tab (zeitgesteuerte Regeln).",
    }


def compute_weekly_review() -> dict:
    try:
        open_tasks = len([t for t in (supabase.table("vt_founder_tasks").select("status").execute().data or []) if t.get("status") in ("neu", "in_bearbeitung", "warten")])
        done_tasks = len([t for t in (supabase.table("vt_founder_tasks").select("status").execute().data or []) if t.get("status") == "erledigt"])
    except Exception:
        open_tasks, done_tasks = None, None

    risks = h_risk_opp.list_executive_risks()
    opportunities = h_risk_opp.list_executive_opportunities()
    module_health = health_module.compute_module_health()
    founder_os_score = combined_score.compute_founder_os_automation_score()
    doc_score = i_score.compute_documentation_score()

    bottlenecks = [g["category"] for g in (g_score.compute_automation_score().get("gaps") or [])][:3]

    return {
        "open_tasks": open_tasks,
        "done_tasks": done_tasks,
        "risks_count": len(risks),
        "opportunities_count": len(opportunities),
        "module_health": module_health,
        "automation_score": founder_os_score.get("overall_percentage"),
        "documentation_coverage": doc_score.get("overall_percentage"),
        "biggest_manual_bottlenecks": bottlenecks,
        "next_automation_opportunities": founder_os_score.get("next_step_towards_90_percent"),
        "release_status_note": "Release-Status siehe Auto-Documentation-/CEO-Intelligence-Tab — hier nicht dupliziert.",
    }
