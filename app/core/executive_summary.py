"""CEO Intelligence — Executive Summary (VitalTwin Enterprise, Founder
Operating System, Submodule H).

Computed fresh on every request ("on read"), exactly like the Founder
Daily Briefing (Submodule B) — no stored/cached summary record, no
scheduler. Answers the 10 questions from the spec using only already-
aggregated data from other Founder-OS modules (never re-deriving its own
copy of the underlying rules).
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import automation_score as automation_score_module
from . import executive_goals
from . import executive_metrics as ex_metrics
from . import executive_risk_opportunity as risk_opp
from .automation_engine import get_daily_briefing_summary
from .supabase import supabase

SEVERITY_ORDER = {"kritisch": 3, "hoch": 2, "mittel": 1, "niedrig": 0}


def _top_n(items: list[dict], *, key: str, n: int = 3) -> list[dict]:
    return sorted(items, key=lambda i: SEVERITY_ORDER.get(i.get(key), 0), reverse=True)[:n]


def compute_executive_summary(period: str = "daily") -> dict:
    overview = ex_metrics.get_ceo_overview()
    risks = risk_opp.list_executive_risks()
    opportunities = risk_opp.list_executive_opportunities()
    goals = executive_goals.list_strategic_goals()
    automation = automation_score_module.compute_automation_score()
    briefing_automation = get_daily_briefing_summary()

    top_risks = _top_n(risks, key="severity")
    top_opportunities = opportunities[:3]
    at_risk_goals = [g for g in goals if (g.get("explanation") or {}).get("at_risk") is True or g.get("status") == "gefaehrdet"]
    on_track_goals = [g for g in goals if (g.get("explanation") or {}).get("on_track") is True]

    open_decisions = overview["open_founder_decisions"]["value"] or 0

    whats_going_well = [f"{g.get('title')} ist im Plan." for g in on_track_goals[:3]]
    if not whats_going_well and (overview["new_users"]["trend"] or 0) > 0:
        whats_going_well.append("Neue Nutzerregistrierungen sind gegenüber der Vorwoche gestiegen.")

    whats_going_badly = [f"{g.get('title')} ist gefährdet." for g in at_risk_goals[:3]]
    if not whats_going_badly and top_risks:
        whats_going_badly.append(f"{len(top_risks)} offene Risiken erkannt.")

    return {
        "period": period,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "whats_going_well": whats_going_well or ["Keine besonderen positiven Auffälligkeiten erkannt."],
        "whats_going_badly": whats_going_badly or ["Keine besonderen negativen Auffälligkeiten erkannt."],
        "whats_changed": [f"Neue Nutzer: Trend {overview['new_users']['trend']}%"] if overview["new_users"]["trend"] is not None else ["Keine signifikante Veränderung erkennbar."],
        "goals_on_track": [{"title": g.get("title")} for g in on_track_goals],
        "goals_at_risk": [{"title": g.get("title")} for g in at_risk_goals],
        "top_risks": top_risks,
        "top_opportunities": top_opportunities,
        "open_founder_decisions": open_decisions,
        "auto_completed_tasks": briefing_automation.get("auto_completed_today"),
        "manual_work_still_high": briefing_automation.get("failed_today", 0) > 0 or (automation.get("overall_percentage") or 0) < 30,
        "automation_percentage": automation.get("overall_percentage"),
    }


def get_ceo_daily_briefing_snippet() -> dict:
    """Small, additive integration for `routers/founder_briefing.py` — the
    compact CEO summary the spec asks Daily Briefing (Submodule B) to
    receive. Never raises: if CEO Intelligence tables/migrations aren't
    available yet, the briefing must still work."""
    try:
        summary = compute_executive_summary("daily")
        top_risk = summary["top_risks"][0]["title"] if summary["top_risks"] else None
        top_opportunity = summary["top_opportunities"][0]["title"] if summary["top_opportunities"] else None
        at_risk_goal = summary["goals_at_risk"][0]["title"] if summary["goals_at_risk"] else None
        return {
            "top_metric": "Neue Nutzer (7 Tage)",
            "biggest_risk": top_risk,
            "biggest_opportunity": top_opportunity,
            "at_risk_goal": at_risk_goal,
            "open_decision_count": summary["open_founder_decisions"],
            "automation_status": summary["automation_percentage"],
        }
    except Exception:
        return {
            "top_metric": None, "biggest_risk": None, "biggest_opportunity": None,
            "at_risk_goal": None, "open_decision_count": None, "automation_status": None,
            "note": "CEO Intelligence noch nicht verfügbar.",
        }
