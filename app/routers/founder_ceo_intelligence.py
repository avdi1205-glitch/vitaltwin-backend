"""CEO Intelligence — API (VitalTwin Enterprise, Founder Operating
System, Submodule H).

Mounted at `/api/admin/founder` in `app/main.py` (own file, same prefix
as every other Founder-OS router). Uses **two new, narrow permissions**
(`view_ceo_intelligence`/`manage_ceo_intelligence`) — per spec, only
`super_admin` (== founder) and the read-only `executive_analyst` role may
ever access this module; `admin` is explicitly excluded from the
automatic grant (see `core/admin_rbac.py`).

**Aggregation layer.** Every endpoint here reads from already-existing
Founder-OS modules (`founder_business_metrics`, `founder_business_goals`,
`automation_score`, `automation_engine`, `affiliate_product_health`) —
see the module docstrings in `core/executive_*.py` for the exact reuse
mapping. No individual Wellness/CGM/Nutrition/Sleep/Movement/Twin-Memory
data ever appears here.
"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, field_validator

from ..core import executive_goals
from ..core import executive_metrics as ex_metrics
from ..core import executive_risk_opportunity as risk_opp
from ..core import executive_scenarios
from ..core import executive_scorecard
from ..core import executive_summary
from ..core.admin_rbac import require_admin_permission
from ..core.ai_usage_logger import log_ai_usage
from ..core.audit import record_audit_event
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services.ai_provider import AIProvider, AIProviderError, OpenAIProvider

router = APIRouter()

QUERY_TABLE = "vt_executive_queries"
GOAL_TABLE = "vt_founder_business_goals"

MAX_QUESTIONS_PER_DAY = 20
MIN_TOTAL_USERS_FOR_ANSWERS = 5
INSUFFICIENT_DATA_MESSAGE = "Dafür sind noch nicht genügend Daten vorhanden."

CEO_SYSTEM_PROMPT = (
    "Du bist ein strategischer Analyse-Assistent fuer den Gruender/CEO von VitalTwin. Du bekommst "
    "ausschliesslich aggregierte, anonymisierte Geschaeftskennzahlen als Kontext (Scorecard, Risiken, "
    "Chancen, Ziele, Kennzahlen). Du darfst NUR auf Basis dieser Daten antworten -- niemals Zahlen "
    "erfinden, niemals einzelne Nutzer erwaehnen, niemals medizinische/Wellness-Daten verwenden (du "
    "bekommst sie ohnehin nie). Du triffst NIEMALS eine Entscheidung -- du bereitest nur vor. Wenn die "
    "gegebenen Daten die Frage nicht beantworten koennen, sag das ehrlich statt zu spekulieren. "
    "Antworte kurz, konkret, auf Deutsch."
)


def _get_ai_provider() -> AIProvider:
    return OpenAIProvider()


class GoalInput(BaseModel):
    title: str
    category: str
    start_value: float | None = None
    target_value: float
    start_date: str | None = None
    target_date: str | None = None
    status: str = "geplant"
    responsible_module: str | None = None
    note: str | None = None


class GoalStatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        allowed = {"geplant", "aktiv", "im_plan", "gefaehrdet", "erreicht", "verfehlt", "pausiert", "archiviert"}
        if value not in allowed:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(allowed))}")
        return value


class ScenarioInput(BaseModel):
    scenario_type: str
    delta_pct: float


class SaveScenarioInput(BaseModel):
    name: str
    scenario_type: str
    delta_pct: float


class AskInput(BaseModel):
    question: str


class SendToTaskInput(BaseModel):
    title: str
    reason: str
    category: str = "business"


class SendToApprovalInput(BaseModel):
    title: str
    reason: str
    category: str = "business"


# ---------------------------------------------------------------------------
# CEO Overview / Strategic KPIs / Scorecard
# ---------------------------------------------------------------------------


@router.get("/ceo-intelligence/overview")
async def ceo_overview(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    risk_opp.detect_data_quality_risk()
    return ex_metrics.get_ceo_overview()


@router.get("/ceo-intelligence/strategic-kpis")
async def strategic_kpis(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    return ex_metrics.get_strategic_kpis()


@router.get("/ceo-intelligence/scorecard")
async def scorecard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    return {"items": executive_scorecard.compute_scorecard()}


# ---------------------------------------------------------------------------
# Strategic Goals (reuses vt_founder_business_goals)
# ---------------------------------------------------------------------------


@router.get("/ceo-intelligence/goals")
async def list_goals(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    return {"items": executive_goals.list_strategic_goals()}


@router.post("/ceo-intelligence/goals")
async def create_goal(data: GoalInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    payload = data.model_dump()
    try:
        response = supabase.table(GOAL_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ziel konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="executive_goal")
    return response.data[0] if response.data else payload


@router.patch("/ceo-intelligence/goals/{goal_id}/status")
async def update_goal_status(goal_id: str, data: GoalStatusInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    supabase.table(GOAL_TABLE).update({"status": data.status, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", goal_id).execute()
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="executive_goal", entity_id=goal_id)
    return {"message": "Status aktualisiert."}


@router.get("/ceo-intelligence/goals/{goal_id}/forecast")
async def goal_forecast(goal_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    rows = supabase.table(GOAL_TABLE).select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden.")
    return executive_goals.forecast_goal(rows[0])


# ---------------------------------------------------------------------------
# Risk Center / Opportunity Center
# ---------------------------------------------------------------------------


@router.get("/ceo-intelligence/risks")
async def list_risks(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    return {"items": risk_opp.list_executive_risks()}


@router.post("/ceo-intelligence/risks/{ref}/send-to-task")
async def send_risk_to_task(ref: str, data: SendToTaskInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    task_id = risk_opp.send_to_task_manager(ref, title=data.title, reason=data.reason, category=data.category)
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="founder_task", metadata={"via": "ceo_intelligence", "ref": ref})
    return {"task_id": task_id}


@router.post("/ceo-intelligence/risks/{ref}/send-to-approval")
async def send_risk_to_approval(ref: str, data: SendToApprovalInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    approval_id = risk_opp.send_to_approval_center(ref, title=data.title, reason=data.reason, category=data.category)
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="founder_approval", metadata={"via": "ceo_intelligence", "ref": ref})
    return {"approval_id": approval_id}


@router.post("/ceo-intelligence/risks/{ref}/close")
async def close_risk(ref: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    try:
        risk_opp.close_executive_risk(ref, closed_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="executive_risk", metadata={"ref": ref, "event": "geschlossen"})
    return {"message": "Risiko geschlossen."}


@router.get("/ceo-intelligence/opportunities")
async def list_opportunities(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    return {"items": risk_opp.list_executive_opportunities()}


@router.post("/ceo-intelligence/opportunities/{ref}/send-to-task")
async def send_opportunity_to_task(ref: str, data: SendToTaskInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    task_id = risk_opp.send_to_task_manager(ref, title=data.title, reason=data.reason, category=data.category)
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="founder_task", metadata={"via": "ceo_intelligence", "ref": ref})
    return {"task_id": task_id}


@router.post("/ceo-intelligence/opportunities/{ref}/send-to-approval")
async def send_opportunity_to_approval(ref: str, data: SendToApprovalInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    approval_id = risk_opp.send_to_approval_center(ref, title=data.title, reason=data.reason, category=data.category)
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="founder_approval", metadata={"via": "ceo_intelligence", "ref": ref})
    return {"approval_id": approval_id}


@router.post("/ceo-intelligence/opportunities/{ref}/archive")
async def archive_opportunity(ref: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    try:
        risk_opp.archive_executive_opportunity(ref, archived_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="executive_opportunity", metadata={"ref": ref, "event": "archiviert"})
    return {"message": "Chance archiviert."}


# ---------------------------------------------------------------------------
# Scenario Planning
# ---------------------------------------------------------------------------


@router.post("/ceo-intelligence/scenarios/simulate")
async def simulate_scenario(data: ScenarioInput, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    try:
        return executive_scenarios.run_scenario(data.scenario_type, delta_pct=data.delta_pct)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ceo-intelligence/scenarios")
async def list_scenarios(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    return {"items": executive_scenarios.list_scenarios()}


@router.post("/ceo-intelligence/scenarios")
async def save_scenario(data: SaveScenarioInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    try:
        scenario = executive_scenarios.save_scenario(name=data.name, scenario_type=data.scenario_type, delta_pct=data.delta_pct, created_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="executive_scenario")
    return scenario


@router.delete("/ceo-intelligence/scenarios/{scenario_id}")
async def delete_scenario(scenario_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    executive_scenarios.delete_scenario(scenario_id)
    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="executive_scenario", entity_id=scenario_id)
    return {"message": "Szenario gelöscht."}


# ---------------------------------------------------------------------------
# Executive Summary
# ---------------------------------------------------------------------------


@router.get("/ceo-intelligence/executive-summary")
async def get_executive_summary(period: str = "daily", authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    if period not in ("daily", "weekly"):
        raise HTTPException(status_code=400, detail="period muss 'daily' oder 'weekly' sein.")
    return executive_summary.compute_executive_summary(period)


# ---------------------------------------------------------------------------
# Frag CEO Intelligence (AI-gestützt, streng datenbasiert)
# ---------------------------------------------------------------------------


@router.post("/ceo-intelligence/ask")
async def ask_ceo_intelligence(data: AskInput, request: Request, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")
    enforce_rate_limit(request, "ceo_intelligence_ask", max_requests=MAX_QUESTIONS_PER_DAY, window_seconds=86400)

    question = data.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Frage darf nicht leer sein.")

    total_users = supabase.table("vt_users").select("*", count="exact").execute().count
    if total_users is None or total_users < MIN_TOTAL_USERS_FOR_ANSWERS:
        _record_query(question=question, answer=None, insufficient_data=True, admin_email=admin.email, ai_provider=None, latency_ms=None, error=None)
        return {"answer": INSUFFICIENT_DATA_MESSAGE, "insufficient_data": True}

    overview = ex_metrics.get_ceo_overview()
    scorecard_items = executive_scorecard.compute_scorecard()
    risks = risk_opp.list_executive_risks()[:5]
    opportunities = risk_opp.list_executive_opportunities()[:5]

    context_lines = [f"{key}: {value.get('value')}" for key, value in overview.items() if isinstance(value, dict)]
    context_lines.append("Scorecard: " + "; ".join(f"{s['area']}={s['status']}" for s in scorecard_items))
    context_lines.append("Top-Risiken: " + ("; ".join(r["title"] for r in risks) or "keine"))
    context_lines.append("Top-Chancen: " + ("; ".join(o["title"] for o in opportunities) or "keine"))
    context_text = f"Frage: {question}\n\nDaten:\n" + "\n".join(context_lines)

    provider = _get_ai_provider()
    start = time.perf_counter()
    try:
        answer = await provider.generate_recommendation_explanation(system_prompt=CEO_SYSTEM_PROMPT, context_text=context_text)
    except AIProviderError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        log_ai_usage(email=admin.email, feature="ceo_intelligence_ask", status="error", error_type=type(exc).__name__, latency_ms=latency_ms)
        _record_query(question=question, answer=None, insufficient_data=False, admin_email=admin.email, ai_provider="openai", latency_ms=latency_ms, error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    latency_ms = int((time.perf_counter() - start) * 1000)
    log_ai_usage(
        email=admin.email, feature="ceo_intelligence_ask", status="success",
        model=getattr(provider, "last_model", None), usage=getattr(provider, "last_usage", None), latency_ms=latency_ms,
    )
    _record_query(question=question, answer=answer, insufficient_data=False, admin_email=admin.email, ai_provider="openai", latency_ms=latency_ms, error=None)
    return {"answer": answer, "insufficient_data": False}


def _record_query(*, question: str, answer: str | None, insufficient_data: bool, admin_email: str, ai_provider: str | None, latency_ms: int | None, error: str | None) -> None:
    try:
        supabase.table(QUERY_TABLE).insert(
            {"question": question, "answer": answer, "insufficient_data": insufficient_data, "ai_provider": ai_provider, "latency_ms": latency_ms, "error": error, "created_by": admin_email}
        ).execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Kostenkontrolle
# ---------------------------------------------------------------------------


@router.get("/ceo-intelligence/cost-control")
async def cost_control(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ceo_intelligence")
    try:
        queries = supabase.table(QUERY_TABLE).select("*").execute().data or []
    except Exception:
        queries = []
    total = len(queries)
    errors = sum(1 for q in queries if q.get("error"))
    return {
        "total_queries": total, "error_count": errors,
        "error_rate": round(errors / total, 3) if total else None,
        "token_usage": None,
        "token_usage_note": "Nicht verfügbar — services/ai_provider.py gibt keinen Token-Verbrauch zurück.",
        "estimated_cost": None,
        "estimated_cost_note": "Nicht verfügbar — kein Kosten-Tracking implementiert.",
        "daily_question_limit": MAX_QUESTIONS_PER_DAY,
    }


# ---------------------------------------------------------------------------
# Export (super_admin/founder only — manage_ceo_intelligence)
# ---------------------------------------------------------------------------


@router.get("/ceo-intelligence/export")
async def export_data(resource: str, format: str = "json", authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_ceo_intelligence")

    resolvers = {
        "scorecard": lambda: executive_scorecard.compute_scorecard(),
        "goals": lambda: executive_goals.list_strategic_goals(),
        "risks": lambda: risk_opp.list_executive_risks(),
        "opportunities": lambda: risk_opp.list_executive_opportunities(),
        "summary": lambda: [executive_summary.compute_executive_summary("weekly")],
        "kpis": lambda: [ex_metrics.get_strategic_kpis()],
    }
    resolver = resolvers.get(resource)
    if resolver is None:
        raise HTTPException(status_code=400, detail=f"Unbekannte Ressource. Erlaubt: {', '.join(sorted(resolvers))}")
    if format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format muss 'csv' oder 'json' sein.")

    rows = resolver()
    record_audit_event(user_id=None, email=admin.email, action="export_request", entity_type="ceo_intelligence_export", metadata={"resource": resource, "format": format})

    if format == "json":
        return {"resource": resource, "items": rows}

    if not rows:
        return {"resource": resource, "csv": ""}
    fieldnames = sorted({key for row in rows for key in row.keys()})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in row.items()})
    return {"resource": resource, "csv": buffer.getvalue()}
