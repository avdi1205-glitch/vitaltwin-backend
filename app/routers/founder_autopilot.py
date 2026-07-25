"""Founder Autopilot — API (VitalTwin Enterprise, Founder Operating
System, Submodule J).

Mounted at `/api/admin/founder`. Uses `view_founder_autopilot`/
`manage_founder_autopilot` — per spec, `manage_founder_autopilot` is
granted to `super_admin` ONLY (no narrow manager role exists for this
submodule, unlike G/H/I), so a permission check alone already enforces
"nur Founder oder Super Admin" for every mutating endpoint here — no
extra role check needed (see `core/admin_rbac.py`). `executive_analyst`
additionally gets `view_founder_autopilot` (read-only).
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..core import automation_opportunity_detector as g_opportunity_detector
from ..core import autopilot_alerts as alerts_module
from ..core import autopilot_events as events_module
from ..core import autopilot_module_health as health_module
from ..core import autopilot_orchestrator as orchestrator
from ..core import autopilot_planning as planning_module
from ..core import autopilot_policies as policies_module
from ..core import autopilot_release_readiness as readiness_module
from ..core import autopilot_score as score_module
from ..core import autopilot_state as state_module
from ..core.admin_rbac import require_admin_permission
from ..core.audit import record_audit_event
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services.ai_provider import AIProvider, AIProviderError, OpenAIProvider

router = APIRouter()

QUERY_TABLE = "vt_founder_autopilot_queries"
MAX_QUESTIONS_PER_DAY = 20
INSUFFICIENT_DATA_MESSAGE = "Dafür sind noch nicht genügend Daten vorhanden."

AUTOPILOT_SYSTEM_PROMPT = (
    "Du bist der Founder-Autopilot-Assistent fuer VitalTwin. Du bekommst ausschliesslich aggregierte "
    "Founder-OS-Daten (Today View, Decision Inbox, Automation Score, Modul-Status). Du darfst NUR auf "
    "Basis dieser Daten antworten -- niemals Zahlen erfinden, niemals einzelne Nutzer erwaehnen. Du "
    "triffst NIEMALS eine Entscheidung selbst -- du fasst nur zusammen. Wenn die Daten die Frage nicht "
    "beantworten koennen, sag das ehrlich. Antworte kurz, konkret, auf Deutsch."
)


def _get_ai_provider() -> AIProvider:
    return OpenAIProvider()


class ModeInput(BaseModel):
    mode: str
    reason: str | None = None


class KillSwitchInput(BaseModel):
    reason: str


class IncidentInput(BaseModel):
    title: str
    reason: str


class PolicyInput(BaseModel):
    name: str
    description: str = ""
    mode: str
    allowed_categories: list[str] = []
    blocked_categories: list[str] = []
    maximum_risk_level: str = "low"
    approval_policy: str = "always_require_approval"
    financial_threshold: float | None = None
    execution_window: dict = {}
    allowed_environments: list[str] = ["production"]
    rollback_required: bool = True
    audit_required: bool = True


class BulkApprovalInput(BaseModel):
    approval_ids: list[str]


class AskInput(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# Home / Today View / Decision Inbox
# ---------------------------------------------------------------------------


@router.get("/autopilot/today-view")
async def today_view(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return orchestrator.get_today_view()


@router.get("/autopilot/decision-inbox")
async def decision_inbox(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return {"items": orchestrator.get_decision_inbox()}


@router.post("/autopilot/decision-inbox/bulk-approve")
async def bulk_approve(data: BulkApprovalInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    try:
        result = orchestrator.execute_one_click_approval(data.approval_ids, decided_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# Mode / Kill Switch / Incident Mode
# ---------------------------------------------------------------------------


@router.get("/autopilot/mode")
async def get_mode(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return state_module.get_current_state()


@router.post("/autopilot/mode")
async def set_mode(data: ModeInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    try:
        return state_module.set_mode(data.mode, reason=data.reason, changed_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/autopilot/kill-switch/activate")
async def activate_kill_switch(data: KillSwitchInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    return state_module.activate_kill_switch(reason=data.reason, activated_by=admin.email)


@router.post("/autopilot/kill-switch/deactivate")
async def deactivate_kill_switch(authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    return state_module.deactivate_kill_switch(deactivated_by=admin.email)


@router.get("/autopilot/incidents")
async def list_incidents(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return {"items": state_module.list_incidents()}


@router.post("/autopilot/incidents/activate")
async def activate_incident(data: IncidentInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    return state_module.activate_incident_mode(title=data.title, reason=data.reason, activated_by=admin.email)


@router.post("/autopilot/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    return state_module.resolve_incident(incident_id, resolved_by=admin.email)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@router.get("/autopilot/policies")
async def list_policies(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return {"items": policies_module.list_policies()}


@router.post("/autopilot/policies")
async def create_policy(data: PolicyInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    try:
        return policies_module.create_policy(data.model_dump(), created_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/autopilot/policies/{policy_id}")
async def update_policy(policy_id: str, data: PolicyInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    try:
        return policies_module.update_policy(policy_id, data.model_dump(), updated_by=admin.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/autopilot/policies/{policy_id}/activate")
async def activate_policy(policy_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    return policies_module.activate_policy(policy_id, activated_by=admin.email)


@router.post("/autopilot/policies/{policy_id}/pause")
async def pause_policy(policy_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    return policies_module.pause_policy(policy_id, paused_by=admin.email)


# ---------------------------------------------------------------------------
# Daily Plan / Weekly Review
# ---------------------------------------------------------------------------


@router.get("/autopilot/daily-plan")
async def daily_plan(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return planning_module.compute_daily_plan()


@router.get("/autopilot/weekly-review")
async def weekly_review(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return planning_module.compute_weekly_review()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/autopilot/alerts")
async def list_alerts(status: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    alerts_module.run_alert_detection()
    return {"items": alerts_module.list_alerts(status=status)}


@router.post("/autopilot/alerts/{alert_id}/close")
async def close_alert(alert_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    alerts_module.close_alert(alert_id)
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="autopilot_alert", entity_id=alert_id, metadata={"event": "geschlossen"})
    return {"message": "Alert geschlossen."}


@router.post("/autopilot/alerts/{alert_id}/escalate")
async def escalate_alert(alert_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    alerts_module.escalate_alert(alert_id)
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="autopilot_alert", entity_id=alert_id, metadata={"event": "eskaliert"})
    return {"message": "Alert eskaliert."}


# ---------------------------------------------------------------------------
# Automation Opportunity Center (reuses Submodule G's detector directly)
# ---------------------------------------------------------------------------


@router.get("/autopilot/opportunities")
async def automation_opportunities(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    g_opportunity_detector.run_opportunity_detection()
    items = supabase.table("vt_automation_opportunities").select("*").order("created_at", desc=True).execute().data or []
    return {"items": items}


# ---------------------------------------------------------------------------
# Automation Score / Work Saved
# ---------------------------------------------------------------------------


@router.get("/autopilot/automation-score")
async def automation_score_endpoint(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return score_module.compute_founder_os_automation_score()


@router.get("/autopilot/work-saved")
async def work_saved(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return score_module.compute_work_saved_estimate()


# ---------------------------------------------------------------------------
# Module Health / Release Readiness
# ---------------------------------------------------------------------------


@router.get("/autopilot/module-health")
async def module_health(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return {"items": health_module.compute_module_health()}


@router.get("/autopilot/release-readiness")
async def release_readiness(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return readiness_module.compute_release_readiness()


# ---------------------------------------------------------------------------
# Orchestration cycle (manual trigger — mirrors Submodule G's run-due)
# ---------------------------------------------------------------------------


@router.post("/autopilot/run-cycle")
async def run_cycle(authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    return orchestrator.run_orchestration_cycle(triggered_by=admin.email)


# ---------------------------------------------------------------------------
# Frag Founder Autopilot
# ---------------------------------------------------------------------------


@router.post("/autopilot/ask")
async def ask_autopilot(data: AskInput, request: Request, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_autopilot")
    enforce_rate_limit(request, "autopilot_ask", max_requests=MAX_QUESTIONS_PER_DAY, window_seconds=86400)

    question = data.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Frage darf nicht leer sein.")

    today = orchestrator.get_today_view()
    if today.get("waiting_approvals") is None and today.get("auto_completed_today") is None and not today.get("entries"):
        _record_query(question=question, answer=None, insufficient_data=True, admin_email=admin.email, error=None)
        return {"answer": INSUFFICIENT_DATA_MESSAGE, "insufficient_data": True}

    score = score_module.compute_founder_os_automation_score()
    context_lines = [
        f"Automatisch erledigt heute: {today.get('auto_completed_today')}",
        f"Fehlgeschlagene Automationen heute: {today.get('failed_automations_today')}",
        f"Wartende Freigaben: {today.get('waiting_approvals')}",
        f"Automatisierungsgrad: {score.get('overall_percentage')}%",
    ]
    context_text = f"Frage: {question}\n\nDaten:\n" + "\n".join(context_lines)

    provider = _get_ai_provider()
    try:
        answer = await provider.generate_recommendation_explanation(system_prompt=AUTOPILOT_SYSTEM_PROMPT, context_text=context_text)
    except AIProviderError as exc:
        _record_query(question=question, answer=None, insufficient_data=False, admin_email=admin.email, error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _record_query(question=question, answer=answer, insufficient_data=False, admin_email=admin.email, error=None)
    return {"answer": answer, "insufficient_data": False}


def _record_query(*, question: str, answer: str | None, insufficient_data: bool, admin_email: str, error: str | None) -> None:
    try:
        supabase.table(QUERY_TABLE).insert(
            {"question": question, "answer": answer, "insufficient_data": insufficient_data, "created_by": admin_email, "error": error}
        ).execute()
    except Exception:
        pass


@router.get("/autopilot/cost-control")
async def cost_control(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_autopilot")
    return {
        "ai_calls_note": "KI wird nur für 'Frag Founder Autopilot' verwendet — alle anderen Funktionen sind regelbasiert.",
        "token_usage": None, "token_usage_note": "Nicht verfügbar — services/ai_provider.py gibt keinen Token-Verbrauch zurück.",
        "estimated_cost": None, "estimated_cost_note": "Nicht verfügbar — kein Kosten-Tracking implementiert.",
        "daily_question_limit": MAX_QUESTIONS_PER_DAY,
        "financial_thresholds_note": "Finanzielle Schwellenwerte werden pro Policy als Konfigurationsfeld gespeichert (financial_threshold) — keine automatische Durchsetzung in Euro, da kein Kosten-Tracking existiert.",
    }
