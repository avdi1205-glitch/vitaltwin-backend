"""Founder Autopilot — Module Health (VitalTwin Enterprise, Founder
Operating System, Submodule J).

Each of the 9 Founder-OS submodules (A-I) is checked with one real,
cheap query against its own core table — `healthy` if reachable,
`critical`/`warning` if recent errors are elevated, `unavailable` if the
query itself fails (e.g. migration not yet run), `not_configured` if no
signal exists at all yet. Every status carries a `reason`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .supabase import supabase

Status = str  # "healthy" | "warning" | "critical" | "unavailable" | "not_configured"


def _safe_count(table: str, *, filters: dict | None = None) -> int | None:
    try:
        query = supabase.table(table).select("*", count="exact")
        for field, value in (filters or {}).items():
            query = query.eq(field, value)
        return query.execute().count
    except Exception:
        return None


def _module(*, key: str, name: str, status: Status, reason: str) -> dict:
    return {"module": key, "name": name, "status": status, "reason": reason}


def compute_module_health() -> list[dict]:
    results = []

    dashboard_ok = _safe_count("vt_users") is not None
    results.append(_module(key="A", name="Founder Dashboard", status="healthy" if dashboard_ok else "unavailable",
                            reason="vt_users erreichbar." if dashboard_ok else "vt_users nicht erreichbar."))

    briefing_ok = _safe_count("vt_founder_tasks") is not None
    results.append(_module(key="B", name="Founder Daily Briefing", status="healthy" if briefing_ok else "unavailable",
                            reason="Kerntabellen erreichbar." if briefing_ok else "Kerntabellen nicht erreichbar."))

    open_tasks = _safe_count("vt_founder_tasks", filters={"status": "neu"})
    task_status = "unavailable" if open_tasks is None else ("warning" if open_tasks > 20 else "healthy")
    results.append(_module(key="C", name="AI Founder Task Manager", status=task_status,
                            reason=f"{open_tasks} neue Aufgaben." if open_tasks is not None else "vt_founder_tasks nicht erreichbar."))

    open_approvals = _safe_count("vt_founder_approvals", filters={"status": "neu"})
    approval_status = "unavailable" if open_approvals is None else ("warning" if open_approvals > 15 else "healthy")
    results.append(_module(key="D", name="Smart Approval Center", status=approval_status,
                            reason=f"{open_approvals} neue Freigaben." if open_approvals is not None else "vt_founder_approvals nicht erreichbar."))

    insights_ok = _safe_count("vt_founder_business_insights") is not None
    results.append(_module(key="E", name="AI Business Coach", status="healthy" if insights_ok else "unavailable",
                            reason="vt_founder_business_insights erreichbar." if insights_ok else "vt_founder_business_insights nicht erreichbar."))

    try:
        broken_links = _safe_count("vt_affiliate_products", filters={"link_status": "broken"})
    except Exception:
        broken_links = None
    affiliate_status = "unavailable" if broken_links is None else ("warning" if broken_links > 5 else "healthy")
    results.append(_module(key="F", name="Affiliate Intelligence", status=affiliate_status,
                            reason=f"{broken_links} defekte Links." if broken_links is not None else "vt_affiliate_products nicht erreichbar."))

    try:
        recent_dead_letters = supabase.table("vt_automation_dead_letters").select(
            "created_at").gte("created_at", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()).execute().data or []
        automation_status = "critical" if len(recent_dead_letters) >= 3 else ("warning" if recent_dead_letters else "healthy")
        automation_reason = f"{len(recent_dead_letters)} endgültig fehlgeschlagene Läufe (24h)."
    except Exception:
        automation_status, automation_reason = "unavailable", "vt_automation_dead_letters nicht erreichbar."
    results.append(_module(key="G", name="Automation Engine", status=automation_status, reason=automation_reason))

    ceo_ok = _safe_count("vt_founder_business_goals") is not None
    results.append(_module(key="H", name="CEO Intelligence", status="healthy" if ceo_ok else "not_configured",
                            reason="vt_founder_business_goals erreichbar (wiederverwendet)." if ceo_ok else "Noch keine Ziele/Datenbasis."))

    docs_ok = _safe_count("vt_documentation_registry")
    doc_status = "unavailable" if docs_ok is None else ("not_configured" if docs_ok == 0 else "healthy")
    results.append(_module(key="I", name="Auto Documentation", status=doc_status,
                            reason=f"{docs_ok} Registry-Einträge." if docs_ok is not None else "vt_documentation_registry nicht erreichbar."))

    return results
