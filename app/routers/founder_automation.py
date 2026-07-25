"""Automation Engine — API (VitalTwin Enterprise, Founder Operating
System, Submodule G).

Mounted at `/api/admin/founder` in `app/main.py` (own file, same prefix
as every other Founder-OS router). Uses **two new, narrow permissions**
(`view_automation_engine`/`manage_automation_engine`) instead of the
shared `view_founder_os`/`manage_founder_os` pair every other Founder-OS
submodule reuses — a deliberate, documented exception: this module can
execute real actions (pausing affiliate products, creating tasks/
approvals) and the spec explicitly requires that normal `admin` accounts
NOT get automatic access, only `super_admin`/`automation_manager`/
`analyst` (read-only). See `core/admin_rbac.py` for the exact matrix.

**No individual Wellness/CGM/Nutrition/Sleep/Movement/Twin-Memory data
ever appears here** — only Founder-OS rules/runs and aggregated business/
system data, exactly like every other Founder-OS submodule.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, field_validator

from ..core import automation_engine as engine
from ..core import automation_opportunity_detector as opportunity_detector
from ..core import automation_registry as registry
from ..core import automation_score as score_module
from ..core.admin_rbac import require_admin_permission
from ..core.audit import record_audit_event
from ..core.concurrency import run_parallel
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services.ai_provider import AIProvider, AIProviderError, OpenAIProvider

router = APIRouter()

RULE_TABLE = "vt_automation_rules"
RUN_TABLE = "vt_automation_runs"
OPPORTUNITY_TABLE = "vt_automation_opportunities"
ALERT_TABLE = "vt_automation_alerts"

ALLOWED_ALERT_STATUSES = {"offen", "bestaetigt", "archiviert"}
MAX_AI_EXPLANATIONS_PER_DAY = 20

FAILURE_EXPLANATION_SYSTEM_PROMPT = (
    "Du bist ein technischer Assistent fuer den Gruender von VitalTwin. Du bekommst ausschliesslich "
    "strukturierte Daten ueber einen fehlgeschlagenen Automatisierungslauf (Regelname, Aktionen, "
    "Fehlermeldungen). Erklaere kurz und konkret auf Deutsch, was vermutlich schiefgelaufen ist und "
    "was der Gruender pruefen sollte. Erfinde niemals Ursachen, die nicht aus den Daten hervorgehen. "
    "Triff niemals eine geschaeftliche, rechtliche oder sicherheitsrelevante Entscheidung."
)


def _get_ai_provider() -> AIProvider:
    return OpenAIProvider()


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class ActionInput(BaseModel):
    action_type: str
    params: dict = {}


class RuleInput(BaseModel):
    name: str
    description: str = ""
    category: str
    trigger_type: str
    trigger_config: dict = {}
    conditions: list[dict] = []
    actions: list[ActionInput]
    risk_level: str
    approval_policy: str = "no_approval"
    retry_policy: dict = {"type": "none", "max_attempts": 1, "cooldown_seconds": 60}
    timeout_seconds: int = 30
    max_runs: int | None = None
    environment: str = "production"
    rollout_stage: str = "nur_founder"

    def to_row(self) -> dict:
        payload = self.model_dump()
        payload["actions"] = [a for a in payload["actions"]]
        return payload


class RuleUpdateInput(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    conditions: list[dict] | None = None
    actions: list[ActionInput] | None = None
    risk_level: str | None = None
    approval_policy: str | None = None
    retry_policy: dict | None = None
    timeout_seconds: int | None = None
    max_runs: int | None = None
    environment: str | None = None
    rollout_stage: str | None = None

    def to_row(self) -> dict:
        payload = {k: v for k, v in self.model_dump().items() if v is not None}
        if "actions" in payload:
            payload["actions"] = [a for a in payload["actions"]]
        return payload


class AlertStatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_ALERT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_ALERT_STATUSES))}")
        return value


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/automation/dashboard")
async def automation_dashboard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    # Must run before the selects below (it can create/update runs that
    # those selects should reflect), so it stays sequential; the 2 selects
    # themselves are independent of each other and run concurrently.
    engine.evaluate_and_run_due_rules()

    def _rules() -> list[dict]:
        try:
            return supabase.table(RULE_TABLE).select("*").execute().data or []
        except Exception:
            return []

    def _runs() -> list[dict]:
        try:
            return supabase.table(RUN_TABLE).select("*").order("created_at", desc=True).limit(200).execute().data or []
        except Exception:
            return []

    rules, runs = run_parallel(_rules, _runs)

    today_start = datetime.now(timezone.utc).date().isoformat()
    runs_today = [r for r in runs if str(r.get("created_at", "")).startswith(today_start)]
    finished = [r for r in runs if r.get("started_at") and r.get("finished_at")]
    durations = []
    for r in finished:
        try:
            start = datetime.fromisoformat(str(r["started_at"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(r["finished_at"]).replace("Z", "+00:00"))
            durations.append((end - start).total_seconds())
        except (ValueError, TypeError):
            continue

    active_rules = [r for r in rules if r.get("status") == "aktiv"]
    paused_rules = [r for r in rules if r.get("status") == "pausiert"]
    risky_rules = [r for r in rules if r.get("risk_level") in ("medium", "high")]
    rules_without_success = [r for r in rules if not r.get("last_run_at")]

    return {
        "active_rules": {"value": len(active_rules), "source": "vt_automation_rules"},
        "paused_rules": {"value": len(paused_rules), "source": "vt_automation_rules"},
        "runs_today": {"value": len(runs_today), "source": "vt_automation_runs"},
        "successful_today": {"value": sum(1 for r in runs_today if r.get("status") == "erfolgreich"), "source": "vt_automation_runs"},
        "failed_today": {"value": sum(1 for r in runs_today if r.get("status") in ("fehlgeschlagen", "dead_letter")), "source": "vt_automation_runs"},
        "awaiting_approval": {"value": sum(1 for r in runs if r.get("status") == "wartet_auf_freigabe"), "source": "vt_automation_runs"},
        "average_runtime_seconds": {"value": round(sum(durations) / len(durations), 1) if durations else None, "source": "vt_automation_runs", "note": None if durations else "Noch keine abgeschlossenen Läufe."},
        "risky_rules": {"value": len(risky_rules), "source": "vt_automation_rules"},
        "rules_without_success": {"value": len(rules_without_success), "source": "vt_automation_rules"},
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/automation/run-due")
async def run_due_automations(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "manage_automation_engine")
    result = engine.evaluate_and_run_due_rules()
    opportunity_detector.run_opportunity_detection()
    return result


# ---------------------------------------------------------------------------
# Safe Action Registry (read-only, powers the Rule Builder UI)
# ---------------------------------------------------------------------------


@router.get("/automation/registry")
async def get_registry(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    return {
        "actions": [
            {"action_type": a.action_type, "label": a.label, "risk_level": a.risk_level, "reversible": a.reversible, "idempotent": a.idempotent, "requires_approval_by_default": a.requires_approval_by_default, "note": a.note}
            for a in registry.ACTION_REGISTRY.values()
        ],
        "categories": sorted(registry.AUTOMATION_CATEGORIES),
        "categories_with_real_actions": sorted(registry.CATEGORIES_WITH_REAL_ACTIONS),
        "trigger_types": sorted(registry.TRIGGER_TYPES),
        "approval_policies": sorted(registry.APPROVAL_POLICIES),
        "not_implemented_note": registry.NOT_IMPLEMENTED_ACTIONS_NOTE,
        "critical_note": registry.CRITICAL_ACTIONS_NOT_IMPLEMENTED_NOTE,
    }


# ---------------------------------------------------------------------------
# Rules CRUD + lifecycle
# ---------------------------------------------------------------------------


@router.get("/automation/rules")
async def list_rules(category: str | None = None, status: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    try:
        items = supabase.table(RULE_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        items = []
    if category:
        items = [i for i in items if i.get("category") == category]
    if status:
        items = [i for i in items if i.get("status") == status]
    return {"items": items}


@router.post("/automation/rules")
async def create_rule(data: RuleInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    try:
        rule = engine.create_rule(data.to_row(), created_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return rule


@router.get("/automation/rules/{rule_id}")
async def get_rule(rule_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    rule = engine.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Regel nicht gefunden.")
    return rule


@router.patch("/automation/rules/{rule_id}")
async def update_rule(rule_id: str, data: RuleUpdateInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    try:
        rule = engine.update_rule(rule_id, data.to_row(), updated_by=admin.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return rule


@router.get("/automation/rules/{rule_id}/versions")
async def get_rule_versions(rule_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    return {"items": engine.list_rule_versions(rule_id)}


@router.post("/automation/rules/{rule_id}/activate")
async def activate_rule(rule_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    rule = engine.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Regel nicht gefunden.")
    if rule.get("risk_level") in ("medium", "high") and admin.role != "super_admin":
        # Low-risk rules may be activated by any automation_manager; medium/
        # high always needs the founder's/super_admin's approval decision —
        # here we still allow *requesting* activation (creates the approval),
        # only the final "freigegeben" decision is restricted (enforced in
        # routers/founder_approval.py's `_apply_entity_side_effect`).
        pass
    try:
        return engine.request_rule_activation(rule_id, requested_by=admin.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/automation/rules/{rule_id}/pause")
async def pause_rule(rule_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    return engine.set_rule_lifecycle_status(rule_id, status="pausiert", enabled=False, updated_by=admin.email)


@router.post("/automation/rules/{rule_id}/archive")
async def archive_rule(rule_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    return engine.set_rule_lifecycle_status(rule_id, status="archiviert", enabled=False, updated_by=admin.email)


@router.post("/automation/rules/{rule_id}/dry-run")
async def dry_run_rule(rule_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "manage_automation_engine")
    try:
        return engine.dry_run_rule(rule_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/automation/rules/{rule_id}/run")
async def run_rule_now(rule_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    try:
        return engine.run_rule_manually(rule_id, requested_by=admin.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/automation/runs")
async def list_runs(rule_id: str | None = None, status: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    try:
        items = supabase.table(RUN_TABLE).select("*").order("created_at", desc=True).limit(200).execute().data or []
    except Exception:
        items = []
    if rule_id:
        items = [i for i in items if i.get("rule_id") == rule_id]
    if status:
        items = [i for i in items if i.get("status") == status]
    return {"items": items}


@router.get("/automation/runs/{run_id}")
async def get_run(run_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    rows = supabase.table(RUN_TABLE).select("*").eq("id", run_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Run nicht gefunden.")
    return rows[0]


@router.post("/automation/runs/{run_id}/rollback")
async def rollback_run(run_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    try:
        return engine.rollback_run(run_id, requested_by=admin.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/automation/runs/{run_id}/explain-failure")
async def explain_failure(run_id: str, request: Request, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "view_automation_engine")
    enforce_rate_limit(request, "automation_explain_failure", max_requests=MAX_AI_EXPLANATIONS_PER_DAY, window_seconds=86400)
    rows = supabase.table(RUN_TABLE).select("*").eq("id", run_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Run nicht gefunden.")
    run = rows[0]
    if run.get("status") not in ("fehlgeschlagen", "dead_letter", "teilweise_erfolgreich", "fehlgeschlagen_wird_wiederholt"):
        raise HTTPException(status_code=400, detail="Nur für fehlgeschlagene/teilweise fehlgeschlagene Läufe verfügbar.")
    context_text = f"Status: {run.get('status')}\nFehler: {run.get('error')}\nSchritte: {run.get('steps')}"
    provider = _get_ai_provider()
    try:
        explanation = await provider.generate_recommendation_explanation(system_prompt=FAILURE_EXPLANATION_SYSTEM_PROMPT, context_text=context_text)
    except AIProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="automation_run", entity_id=run_id, metadata={"event": "ki_erklaerung_angefordert"})
    return {"explanation": explanation}


# ---------------------------------------------------------------------------
# Automation Opportunity Detection (suggestions only — never auto-activated)
# ---------------------------------------------------------------------------


@router.get("/automation/opportunities")
async def list_opportunities(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    opportunity_detector.run_opportunity_detection()
    items = supabase.table(OPPORTUNITY_TABLE).select("*").order("created_at", desc=True).execute().data or []
    return {"items": items}


@router.post("/automation/opportunities/{opportunity_id}/dismiss")
async def dismiss_opportunity(opportunity_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    opportunity_detector.dismiss_opportunity(opportunity_id)
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="automation_opportunity", entity_id=opportunity_id, metadata={"event": "abgelehnt"})
    return {"message": "Vorschlag verworfen."}


@router.post("/automation/opportunities/{opportunity_id}/create-rule")
async def create_rule_from_opportunity(opportunity_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    rows = supabase.table(OPPORTUNITY_TABLE).select("*").eq("id", opportunity_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden.")
    opportunity = rows[0]
    suggested = opportunity.get("suggested_rule") or {}
    draft_payload = {
        "name": suggested.get("name", "Neue Regel"),
        "description": opportunity.get("description", ""),
        "category": suggested.get("category", "founder_tasks"),
        "trigger_type": suggested.get("trigger_type", "manual"),
        "trigger_config": {},
        "conditions": [],
        "actions": suggested.get("actions", [{"action_type": "task_erstellen", "params": {}}]),
        "risk_level": suggested.get("risk_level", "low"),
        "approval_policy": suggested.get("approval_policy", "no_approval"),
        "retry_policy": {"type": "none", "max_attempts": 1, "cooldown_seconds": 60},
        "timeout_seconds": 30,
        "environment": "production",
        "rollout_stage": "nur_founder",
    }
    try:
        rule = engine.create_rule(draft_payload, created_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    opportunity_detector.mark_opportunity_rule_created(opportunity_id)
    return {"message": "Entwurf erstellt (deaktiviert, muss noch aktiviert werden).", "rule": rule}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/automation/alerts")
async def list_alerts(status: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    items = supabase.table(ALERT_TABLE).select("*").order("created_at", desc=True).execute().data or []
    if status:
        items = [i for i in items if i.get("status") == status]
    return {"items": items}


@router.patch("/automation/alerts/{alert_id}/status")
async def update_alert_status(alert_id: str, data: AlertStatusInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_automation_engine")
    supabase.table(ALERT_TABLE).update({"status": data.status, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", alert_id).execute()
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="automation_alert", entity_id=alert_id, metadata={"status": data.status})
    return {"message": "Status aktualisiert."}


# ---------------------------------------------------------------------------
# Automation Score
# ---------------------------------------------------------------------------


@router.get("/automation/automation-score")
async def automation_score_endpoint(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    return score_module.compute_automation_score()


# ---------------------------------------------------------------------------
# Kostenkontrolle (KI-Aufrufe dieses Moduls — ehrlich, kein Fake-Tracking)
# ---------------------------------------------------------------------------


@router.get("/automation/cost-control")
async def cost_control(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_automation_engine")
    return {
        "ai_calls_available": True,
        "ai_calls_note": "KI wird nur für 'Fehler erklären' (POST .../runs/{id}/explain-failure) verwendet — alle anderen Funktionen sind regelbasiert, ohne LLM-Aufruf.",
        "token_usage": None,
        "token_usage_note": "Nicht verfügbar — die bestehende AIProvider-Abstraktion (services/ai_provider.py) gibt keinen Token-Verbrauch zurück.",
        "estimated_cost": None,
        "estimated_cost_note": "Nicht verfügbar — kein Kosten-Tracking implementiert (siehe token_usage_note).",
        "daily_explanation_limit": MAX_AI_EXPLANATIONS_PER_DAY,
        "saved_ai_calls_note": "Alle Trigger-/Bedingungs-/Aktionsauswertungen laufen regelbasiert ohne KI-Aufruf — es gibt daher keine 'eingesparten' KI-Aufrufe im Sinne einer Zählung, weil hierfür nie ein KI-Aufruf vorgesehen war.",
    }
