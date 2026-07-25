"""AI Business Coach — API (VitalTwin Enterprise, Founder Operating
System, Submodule E).

Mounted at `/api/admin/founder` in `app/main.py` (own file, same prefix as
every other Founder-OS router — module isolation, one mount point).
Reuses the existing `view_founder_os`/`manage_founder_os` permissions —
deliberately no new fragmented permission pair (per the consolidation
after Release F3, and the explicit "keine doppelte Arbeit" instruction).

**No individual user data ever leaves this router.** Every response is
built from aggregated counts/sums (see `core/founder_business_metrics.py`)
— never a raw user row, never Wellness/CGM/Nutrition/Sleep/Movement/
Twin-Memory data.

**No LLM call for insight detection** (see
`core/founder_business_insight_engine.py`) — the AI provider is used
*only* for the free-text "Frag deinen Business Coach" endpoint below, and
even there strictly grounded in pre-aggregated numbers, never raw rows.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, field_validator

from ..core import founder_business_goals as goals_module
from ..core import founder_business_insight_engine as insight_engine
from ..core import founder_business_metrics as metrics
from ..core.admin_rbac import require_admin_permission
from ..core.audit import record_audit_event
from ..core.concurrency import run_parallel
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services.ai_provider import AIProvider, AIProviderError, OpenAIProvider

router = APIRouter()

GOAL_TABLE = "vt_founder_business_goals"
INSIGHT_TABLE = "vt_founder_business_insights"
RECOMMENDATION_TABLE = "vt_founder_business_recommendations"
COACH_QUERY_TABLE = "vt_founder_coach_queries"
AUTOMATION_EVENT_TABLE = "vt_founder_automation_events"
TASK_TABLE = "vt_founder_tasks"
APPROVAL_TABLE = "vt_founder_approvals"

ALLOWED_GOAL_CATEGORIES = {
    "monatsumsatz", "premium_abos", "aktive_nutzer", "conversion_rate", "affiliate_umsatz",
    "kuendigungsrate", "ki_kostenlimit", "veroeffentlichte_inhalte", "individuell",
    # Additiv für CEO Intelligence (Submodul H) ergänzt — dieselbe Tabelle
    # (vt_founder_business_goals) wird als "Strategic Goals" wiederverwendet,
    # keine parallele Ziel-Tabelle.
    "automatisierungsziel", "release_ziel", "internationales_wachstum",
}
ALLOWED_GOAL_STATUSES = {"geplant", "aktiv", "gefaehrdet", "erreicht", "pausiert", "archiviert"}
ALLOWED_INSIGHT_STATUSES = {"erkannt", "geprueft", "als_aufgabe_erstellt", "zur_freigabe_gesendet", "umgesetzt", "verworfen", "archiviert"}

MAX_COACH_QUESTIONS_PER_DAY = 20
MIN_TOTAL_USERS_FOR_COACH = metrics.MIN_GROUP_SIZE

INSUFFICIENT_DATA_MESSAGE = "Für diese Frage sind noch nicht genügend Daten vorhanden."

BUSINESS_COACH_SYSTEM_PROMPT = (
    "Du bist ein Business-Analyse-Assistent für den Gruender von VitalTwin. "
    "Du bekommst ausschliesslich aggregierte, anonymisierte Geschaeftskennzahlen als Kontext. "
    "Du darfst NUR auf Basis dieser Zahlen antworten -- niemals Zahlen erfinden, niemals einzelne "
    "Nutzer erwaehnen, niemals medizinische/Wellness-Daten einzelner Nutzer verwenden (du bekommst "
    "sie ohnehin nie). Wenn die gegebenen Daten die Frage nicht beantworten koennen, sag das ehrlich "
    "statt zu spekulieren. Antworte kurz, konkret, auf Deutsch."
)


def _get_ai_provider() -> AIProvider:
    """Factory, not a module-level singleton — matches the convention in
    `routers/chat.py::_get_ai_provider`, easy to monkeypatch in tests."""
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

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        if value not in ALLOWED_GOAL_CATEGORIES:
            raise ValueError(f"Ungültige Kategorie. Erlaubt: {', '.join(sorted(ALLOWED_GOAL_CATEGORIES))}")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_GOAL_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_GOAL_STATUSES))}")
        return value


class GoalStatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_GOAL_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_GOAL_STATUSES))}")
        return value


class InsightStatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_INSIGHT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_INSIGHT_STATUSES))}")
        return value


class RecommendationInput(BaseModel):
    insight_id: str | None = None
    title: str
    reasoning: str
    data_basis: str
    expected_benefit: str | None = None
    risk: str | None = None
    effort: str | None = None
    priority: str = "mittel"
    success_metric: str | None = None
    test_period: str | None = None


class AskInput(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/business-coach/dashboard")
async def business_coach_dashboard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    # get_business_dashboard() doesn't depend on insight detection's
    # writes, so run it concurrently with detection instead of after it.
    # The insights select below DOES depend on detection having finished
    # (it must see freshly-written rows), so that one stays sequential.
    _, dashboard = run_parallel(insight_engine.run_insight_detection, metrics.get_business_dashboard)

    try:
        insights = supabase.table(INSIGHT_TABLE).select("category,status,severity").execute().data or []
    except Exception:
        insights = []
    open_insight_statuses = {"erkannt", "geprueft", "zur_freigabe_gesendet"}
    opportunities = [i for i in insights if "chance" in (i.get("category") or "") and i.get("status") in open_insight_statuses]
    risks = [i for i in insights if "risiko" in (i.get("category") or "") and i.get("status") in open_insight_statuses]

    dashboard["open_opportunities"]["value"] = len(opportunities)
    dashboard["open_risks"]["value"] = len(risks)

    return dashboard


# ---------------------------------------------------------------------------
# Insights (deckt auch Chancen/Risiken ab — per Kategorie-Filter)
# ---------------------------------------------------------------------------


@router.get("/business-coach/insights")
async def list_insights(category: str | None = None, status: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        items = supabase.table(INSIGHT_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        items = []
    if category:
        items = [i for i in items if i.get("category") == category]
    if status:
        items = [i for i in items if i.get("status") == status]
    return {"items": items}


@router.get("/business-coach/opportunities")
async def list_opportunities(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        items = supabase.table(INSIGHT_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        items = []
    return {"items": [i for i in items if "chance" in (i.get("category") or "")]}


@router.get("/business-coach/risks")
async def list_risks(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        items = supabase.table(INSIGHT_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        items = []
    return {"items": [i for i in items if "risiko" in (i.get("category") or "")]}


@router.get("/business-coach/insights/{insight_id}/why")
async def explain_insight(insight_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        rows = supabase.table(INSIGHT_TABLE).select("*").eq("id", insight_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Insight nicht gefunden.")
    insight = rows[0]
    return {
        "data_used": insight.get("data_basis"),
        "period": {"start": insight.get("period_start"), "end": insight.get("period_end")},
        "compared_with": {"start": insight.get("comparison_period_start"), "end": insight.get("comparison_period_end")},
        "calculation": insight.get("description"),
        "confidence": insight.get("confidence"),
        "observation_or_assumption": "Beobachtung (aus tatsächlich gemessenen Werten), keine Vermutung.",
        "missing_data": "Keine — alle verwendeten Werte stammen aus den genannten echten Tabellen." if insight.get("source") == "regelbasiert" else "Unbekannt.",
        "source_references": insight.get("source_references"),
    }


@router.patch("/business-coach/insights/{insight_id}/status")
async def update_insight_status(insight_id: str, data: InsightStatusInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    try:
        supabase.table(INSIGHT_TABLE).update(
            {"status": data.status, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", insight_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Status konnte nicht geändert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="business_insight", entity_id=insight_id)
    return {"message": "Status aktualisiert."}


@router.post("/business-coach/insights/{insight_id}/send-to-tasks")
async def send_insight_to_tasks(insight_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    try:
        rows = supabase.table(INSIGHT_TABLE).select("*").eq("id", insight_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Insight nicht gefunden.")
    result = insight_engine.send_insight_to_task_manager(rows[0], admin_email=admin.email)
    if result is None:
        return {"message": "Bereits an den Task Manager übergeben (keine Duplikate)."}
    return {"message": "An den AI Founder Task Manager übergeben.", "task": result}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@router.get("/business-coach/recommendations")
async def list_recommendations(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        items = supabase.table(RECOMMENDATION_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        items = []
    return {"items": items}


@router.post("/business-coach/recommendations")
async def create_recommendation(data: RecommendationInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    payload = data.model_dump()
    payload["status"] = "offen"
    try:
        response = supabase.table(RECOMMENDATION_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Empfehlung konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="business_recommendation")
    return response.data[0] if response.data else payload


@router.post("/business-coach/recommendations/{recommendation_id}/send-to-approval")
async def send_recommendation_to_approval(recommendation_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    try:
        rows = supabase.table(RECOMMENDATION_TABLE).select("*").eq("id", recommendation_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Empfehlung nicht gefunden.")
    result = insight_engine.send_recommendation_to_approval_center(rows[0], admin_email=admin.email)
    if result is None:
        return {"message": "Bereits an das Smart Approval Center übergeben (keine Duplikate)."}
    return {"message": "An das Smart Approval Center übergeben — Freigabe erfolgt dort durch den Gründer.", "approval": result}


# ---------------------------------------------------------------------------
# Business Goals
# ---------------------------------------------------------------------------


@router.get("/business-coach/goals")
async def list_goals(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        goals = supabase.table(GOAL_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        goals = []

    for goal in goals:
        current_value, note = goals_module.compute_goal_progress(goal)
        goal["current_progress"] = current_value
        goal["progress_note"] = note
        goal["explanation"] = goals_module.explain_goal_progress(goal, current_value)
    return {"items": goals}


@router.post("/business-coach/goals")
async def create_goal(data: GoalInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    payload = data.model_dump()
    payload["data_source"] = "wird automatisch berechnet, falls Kategorie unterstützt wird" if data.category in {
        "premium_abos", "aktive_nutzer", "conversion_rate", "affiliate_umsatz", "veroeffentlichte_inhalte",
    } else "nicht verbunden — manuelle Pflege erforderlich"
    try:
        response = supabase.table(GOAL_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ziel konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="business_goal")
    return response.data[0] if response.data else payload


@router.patch("/business-coach/goals/{goal_id}/status")
async def update_goal_status(goal_id: str, data: GoalStatusInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    try:
        supabase.table(GOAL_TABLE).update(
            {"status": data.status, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", goal_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Status konnte nicht geändert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="business_goal", entity_id=goal_id)
    return {"message": "Status aktualisiert."}


# ---------------------------------------------------------------------------
# Frag deinen Business Coach (AI-gestützt, streng datenbasiert)
# ---------------------------------------------------------------------------


@router.post("/business-coach/ask")
async def ask_business_coach(data: AskInput, request: Request, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    enforce_rate_limit(request, "business_coach_ask", max_requests=MAX_COACH_QUESTIONS_PER_DAY, window_seconds=86400)

    question = data.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Frage darf nicht leer sein.")

    dashboard = metrics.get_business_dashboard()
    total_users = metrics.count_rows("vt_users")
    if total_users is None or total_users < MIN_TOTAL_USERS_FOR_COACH:
        _record_query(question=question, answer=None, insufficient_data=True, admin_email=admin.email, ai_provider=None, latency_ms=None, error=None)
        return {"answer": INSUFFICIENT_DATA_MESSAGE, "insufficient_data": True}

    try:
        insights = supabase.table(INSIGHT_TABLE).select("title,category,description,severity,confidence").order("created_at", desc=True).limit(10).execute().data or []
    except Exception:
        insights = []

    context_lines = [f"{key}: {value.get('value')}" for key, value in dashboard.items() if isinstance(value, dict)]
    context_lines.append("Aktuelle Insights: " + ("; ".join(f"{i['title']} ({i['category']})" for i in insights) or "keine"))
    context_text = f"Frage: {question}\n\nDaten:\n" + "\n".join(context_lines)

    provider = _get_ai_provider()
    start = time.perf_counter()
    try:
        answer = await provider.generate_recommendation_explanation(
            system_prompt=BUSINESS_COACH_SYSTEM_PROMPT, context_text=context_text
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
    except AIProviderError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        _record_query(
            question=question, answer=None, insufficient_data=False, admin_email=admin.email,
            ai_provider="openai", latency_ms=latency_ms, error=str(exc),
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _record_query(
        question=question, answer=answer, insufficient_data=False, admin_email=admin.email,
        ai_provider="openai", latency_ms=latency_ms, error=None,
    )
    return {"answer": answer, "insufficient_data": False}


def _record_query(
    *, question: str, answer: str | None, insufficient_data: bool, admin_email: str,
    ai_provider: str | None, latency_ms: int | None, error: str | None,
) -> None:
    try:
        supabase.table(COACH_QUERY_TABLE).insert(
            {
                "question": question,
                "answer": answer,
                "insufficient_data": insufficient_data,
                "ai_provider": ai_provider,
                "latency_ms": latency_ms,
                "error": error,
                "created_by": admin_email,
            }
        ).execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Kostenkontrolle
# ---------------------------------------------------------------------------


@router.get("/business-coach/cost-control")
async def cost_control_stats(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        queries = supabase.table(COACH_QUERY_TABLE).select("*").execute().data or []
    except Exception:
        queries = []

    total = len(queries)
    errors = sum(1 for q in queries if q.get("error"))
    latencies = [q["latency_ms"] for q in queries if isinstance(q.get("latency_ms"), int)]
    avg_latency = round(sum(latencies) / len(latencies)) if latencies else None

    return {
        "total_queries": total,
        "error_count": errors,
        "error_rate": round(errors / total, 3) if total else None,
        "average_latency_ms": avg_latency,
        "token_usage": None,
        "token_usage_note": "Nicht verfügbar — die bestehende AIProvider-Abstraktion (services/ai_provider.py) gibt keinen Token-Verbrauch zurück.",
        "estimated_cost": None,
        "estimated_cost_note": "Nicht verfügbar — kein Kosten-Tracking implementiert (siehe token_usage_note).",
        "daily_question_limit": MAX_COACH_QUESTIONS_PER_DAY,
    }


# ---------------------------------------------------------------------------
# Automation Score
# ---------------------------------------------------------------------------


@router.get("/business-coach/automation-score")
async def automation_score(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        events = supabase.table(AUTOMATION_EVENT_TABLE).select("event_type").execute().data or []
    except Exception:
        events = []

    auto_insights = sum(1 for e in events if e.get("event_type") == "insight_erkannt")
    auto_tasks = sum(1 for e in events if e.get("event_type") == "aufgabe_erstellt")
    auto_approvals_prepared = sum(1 for e in events if e.get("event_type") == "freigabe_vorbereitet")

    try:
        open_tasks = len([t for t in (supabase.table(TASK_TABLE).select("status").execute().data or []) if t.get("status") in ("neu", "in_bearbeitung", "warten")])
    except Exception:
        open_tasks = 0
    try:
        open_approvals = len([a for a in (supabase.table(APPROVAL_TABLE).select("status").execute().data or []) if a.get("status") in ("neu", "ki_geprueft", "zur_pruefung")])
    except Exception:
        open_approvals = 0

    manual_decisions_required = open_tasks + open_approvals
    total_events = auto_insights + auto_tasks + auto_approvals_prepared + manual_decisions_required
    automation_pct = round((auto_insights + auto_tasks + auto_approvals_prepared) / total_events * 100) if total_events else None

    return {
        "auto_detected_insights": auto_insights,
        "auto_created_tasks": auto_tasks,
        "auto_prepared_approvals": auto_approvals_prepared,
        "manual_decisions_required": manual_decisions_required,
        "automation_percentage": automation_pct,
        "note": "Berechnet aus vt_founder_automation_events + offenen Tasks/Approvals — kein fester Wert." if total_events else "Noch keine Prozessdaten vorhanden.",
    }
