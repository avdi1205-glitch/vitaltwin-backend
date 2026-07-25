"""Auto Documentation — API (VitalTwin Enterprise, Founder Operating
System, Submodule I).

Mounted at `/api/admin/founder` in `app/main.py`. Uses two new, narrow
permissions (`view_documentation`/`manage_documentation`) — `developer`
and the new `documentation_editor` role get both; `admin` is explicitly
excluded (see `core/admin_rbac.py`). Founder-only actions (approving a
protected-document proposal happens via the Approval Center, but
archiving a registry entry and changing publish policy are gated with an
additional `role == "super_admin"` check here, per spec: "Nur Founder
oder Super Admin dürfen ... Dokumente endgültig archivieren").
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..core import changelog_engine
from ..core import documentation_change_proposals as proposals_module
from ..core import documentation_generation as generation_module
from ..core import documentation_registry as registry_module
from ..core import documentation_scanner as scanner
from ..core import documentation_score as score_module
from ..core import documentation_search as search_module
from ..core import documentation_stale_detection as stale_module
from ..core import release_notes_engine
from ..core.admin_rbac import require_admin_permission
from ..core.audit import record_audit_event
from ..core.concurrency import run_parallel
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services.ai_provider import AIProvider, AIProviderError, OpenAIProvider

router = APIRouter()

QUERY_TABLE = "vt_documentation_queries"
RUN_TABLE = "vt_documentation_generation_runs"

MAX_QUESTIONS_PER_DAY = 20
INSUFFICIENT_DATA_MESSAGE = "Diese Information ist aktuell nicht dokumentiert."

DOC_ASSISTANT_SYSTEM_PROMPT = (
    "Du bist ein Dokumentations-Assistent fuer das VitalTwin-Projekt. Du bekommst ausschliesslich "
    "bereits registrierte Dokumentations-Metadaten und Scan-Ergebnisse (Routen, Datenmodelle, "
    "Migrationen) als Kontext. Du darfst NUR auf Basis dieser Daten antworten -- niemals Funktionen "
    "erfinden, die nicht im Kontext vorkommen. Wenn die Daten die Frage nicht beantworten koennen, "
    "sag das ehrlich statt zu spekulieren. Antworte kurz, konkret, auf Deutsch."
)


def _get_ai_provider() -> AIProvider:
    return OpenAIProvider()


def _require_founder(admin) -> None:
    if admin.role != "super_admin":
        raise HTTPException(status_code=403, detail="Diese Aktion ist Founder/Super Admin vorbehalten.")


class RegisterDocumentInput(BaseModel):
    document_path: str
    title: str
    category: str
    module: str = "founder_os"
    submodule: str | None = None
    owner: str | None = None
    status: str = "draft"
    source_files: list[str] = []


class ChangeProposalInput(BaseModel):
    registry_id: str
    document_path: str
    proposed_content: str
    reason: str
    risk_level: str = "hoch"


class RollbackInput(BaseModel):
    target_version: int


class AskInput(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/documentation/dashboard")
async def documentation_dashboard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    registry_module.seed_known_documents()

    documents = registry_module.list_documents()
    stale = [d for d in documents if d.get("status") == "stale"]
    pending_review = [d for d in documents if d.get("status") == "pending_review"]

    def _runs() -> list[dict]:
        try:
            return supabase.table(RUN_TABLE).select("*").order("created_at", desc=True).limit(20).execute().data or []
        except Exception:
            return []

    # These 4 are independent of each other (and of the `documents` list
    # above, which only needed `seed_known_documents()` to have finished
    # first) — run them concurrently.
    missing, runs, doc_score, automation_score = run_parallel(
        stale_module.detect_missing_documentation,
        _runs,
        score_module.compute_documentation_score,
        score_module.compute_documentation_automation_score,
    )
    last_run = runs[0] if runs else None
    last_successful_run = next((r for r in runs if r.get("status") == "erfolgreich"), None)
    failed_runs = sum(1 for r in runs if r.get("status") == "fehlgeschlagen")

    api_files_documented = {f for d in documents if d.get("category") == "api" for f in (d.get("source_files") or [])}
    all_api_files = {r["router_file"] for r in scanner.scan_api_routes()}
    undocumented_apis = len(all_api_files - api_files_documented)

    model_files_documented = {f for d in documents if d.get("category") == "datenmodelle" for f in (d.get("source_files") or [])}
    all_model_files = {t["migration_file"] for t in scanner.scan_data_models()}
    undocumented_models = len(all_model_files - model_files_documented)

    migration_files_documented = {f for d in documents if d.get("category") == "migrationen" for f in (d.get("source_files") or [])}
    all_migration_files = {m["file"] for m in scanner.scan_migrations()}
    undocumented_migrations = len(all_migration_files - migration_files_documented)

    return {
        "total_documents": len(documents),
        "current_documents": len([d for d in documents if d.get("status") == "current"]),
        "possibly_stale_documents": len(stale),
        "missing_documentation_count": len(missing),
        "open_documentation_tasks": None,  # siehe Task Manager (category='technik', source='auto_documentation')
        "pending_review_documents": len(pending_review),
        "last_automatic_update": last_run.get("finished_at") if last_run else None,
        "last_successful_run": last_successful_run.get("finished_at") if last_successful_run else None,
        "failed_runs": failed_runs,
        "undocumented_apis": undocumented_apis,
        "undocumented_data_models": undocumented_models,
        "undocumented_migrations": undocumented_migrations,
        "documentation_coverage_percentage": doc_score.get("overall_percentage"),
        "automation_percentage": automation_score.get("automation_percentage"),
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@router.get("/documentation/registry")
async def list_registry(category: str | None = None, status: str | None = None, module: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": registry_module.list_documents(category=category, status=status, module=module)}


@router.post("/documentation/registry")
async def register_document(data: RegisterDocumentInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    payload = data.model_dump()
    saved = registry_module.register_document(payload, created_by=admin.email)
    return saved


@router.get("/documentation/registry/{registry_id}")
async def get_registry_entry(registry_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    doc = registry_module.get_document(registry_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden.")
    return doc


@router.get("/documentation/registry/{registry_id}/versions")
async def get_registry_versions(registry_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": registry_module.list_versions(registry_id)}


@router.post("/documentation/registry/{registry_id}/rollback")
async def rollback_document(registry_id: str, data: RollbackInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    try:
        return registry_module.rollback_document(registry_id, target_version=data.target_version, rolled_back_by=admin.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/documentation/registry/{registry_id}/review")
async def mark_reviewed(registry_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    registry_module.mark_reviewed(registry_id, reviewed_by=admin.email)
    return {"message": "Als geprüft markiert."}


@router.post("/documentation/registry/{registry_id}/archive")
async def archive_document(registry_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    _require_founder(admin)  # "Nur Founder oder Super Admin dürfen ... Dokumente endgültig archivieren"
    registry_module.archive_document(registry_id, archived_by=admin.email)
    return {"message": "Archiviert."}


# ---------------------------------------------------------------------------
# Generation / Stale / Missing
# ---------------------------------------------------------------------------


@router.post("/documentation/generate")
async def generate(authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    return generation_module.run_generation(run_type="manuell", triggered_by=admin.email)


@router.get("/documentation/generation-runs")
async def list_generation_runs(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    try:
        items = supabase.table(RUN_TABLE).select("*").order("created_at", desc=True).limit(50).execute().data or []
    except Exception:
        items = []
    return {"items": items}


@router.get("/documentation/stale")
async def list_stale(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": stale_module.detect_stale_documents()}


@router.get("/documentation/missing")
async def list_missing(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": stale_module.detect_missing_documentation()}


# ---------------------------------------------------------------------------
# Protected documents / Change Proposals
# ---------------------------------------------------------------------------


@router.post("/documentation/proposals")
async def create_proposal(data: ChangeProposalInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    try:
        proposal = proposals_module.create_change_proposal(
            registry_id=data.registry_id, document_path=data.document_path, proposed_content=data.proposed_content,
            reason=data.reason, risk_level=data.risk_level, created_by=admin.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal


@router.post("/documentation/proposals/{proposal_id}/send-to-approval")
async def send_proposal_to_approval(proposal_id: str, document_path: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    approval_id = proposals_module.send_proposal_to_approval_center(proposal_id, document_path=document_path, sent_by=admin.email)
    return {"approval_id": approval_id}


@router.get("/documentation/proposals")
async def list_proposals(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    items = supabase.table("vt_documentation_change_proposals").select("*").order("created_at", desc=True).execute().data or []
    return {"items": items}


# ---------------------------------------------------------------------------
# Live scans (API / Data Models / Migrations)
# ---------------------------------------------------------------------------


@router.get("/documentation/apis")
async def list_apis(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": scanner.scan_api_routes()}


@router.get("/documentation/data-models")
async def list_data_models(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": scanner.scan_data_models()}


@router.get("/documentation/migrations")
async def list_migrations(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": scanner.scan_migrations()}


@router.get("/documentation/services")
async def list_services(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": scanner.scan_core_services()}


# ---------------------------------------------------------------------------
# Changelog / Release Notes
# ---------------------------------------------------------------------------


@router.get("/documentation/changelog/draft")
async def changelog_draft(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return changelog_engine.generate_changelog_draft()


@router.get("/documentation/release-notes/internal")
async def internal_release_notes(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return release_notes_engine.generate_internal_release_notes()


@router.get("/documentation/release-notes/user")
async def user_release_notes(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return release_notes_engine.generate_user_release_notes()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get("/documentation/search")
async def search(q: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return {"items": search_module.search_documents(q)}


# ---------------------------------------------------------------------------
# Documentation Score / Automation Score
# ---------------------------------------------------------------------------


@router.get("/documentation/score")
async def documentation_score_endpoint(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return score_module.compute_documentation_score()


@router.get("/documentation/automation-score")
async def documentation_automation_score_endpoint(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    return score_module.compute_documentation_automation_score()


# ---------------------------------------------------------------------------
# Frag die Projektdokumentation (AI-gestützt)
# ---------------------------------------------------------------------------


@router.post("/documentation/ask")
async def ask_documentation(data: AskInput, request: Request, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_documentation")
    enforce_rate_limit(request, "documentation_ask", max_requests=MAX_QUESTIONS_PER_DAY, window_seconds=86400)

    question = data.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Frage darf nicht leer sein.")

    documents = registry_module.list_documents()
    if not documents:
        _record_query(question=question, answer=None, insufficient_data=True, admin_email=admin.email, ai_provider=None, error=None)
        return {"answer": INSUFFICIENT_DATA_MESSAGE, "insufficient_data": True}

    context_lines = [f"{d.get('title')} ({d.get('category')}, Status: {d.get('status')})" for d in documents[:30]]
    context_lines.append("APIs: " + "; ".join(f"{r['method']} {r['path']}" for r in scanner.scan_api_routes()[:20]))
    context_lines.append("Migrationen: " + "; ".join(m["file"] for m in scanner.scan_migrations()[-10:]))
    context_text = f"Frage: {question}\n\nDokumentation:\n" + "\n".join(context_lines)

    provider = _get_ai_provider()
    try:
        answer = await provider.generate_recommendation_explanation(system_prompt=DOC_ASSISTANT_SYSTEM_PROMPT, context_text=context_text)
    except AIProviderError as exc:
        _record_query(question=question, answer=None, insufficient_data=False, admin_email=admin.email, ai_provider="openai", error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _record_query(question=question, answer=answer, insufficient_data=False, admin_email=admin.email, ai_provider="openai", error=None)
    return {"answer": answer, "insufficient_data": False}


def _record_query(*, question: str, answer: str | None, insufficient_data: bool, admin_email: str, ai_provider: str | None, error: str | None) -> None:
    try:
        supabase.table(QUERY_TABLE).insert(
            {"question": question, "answer": answer, "insufficient_data": insufficient_data, "ai_provider": ai_provider, "error": error, "created_by": admin_email}
        ).execute()
    except Exception:
        pass


@router.get("/documentation/cost-control")
async def cost_control(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_documentation")
    try:
        queries = supabase.table(QUERY_TABLE).select("*").execute().data or []
    except Exception:
        queries = []
    total = len(queries)
    errors = sum(1 for q in queries if q.get("error"))
    return {
        "total_queries": total, "error_count": errors,
        "error_rate": round(errors / total, 3) if total else None,
        "token_usage": None, "token_usage_note": "Nicht verfügbar — services/ai_provider.py gibt keinen Token-Verbrauch zurück.",
        "estimated_cost": None, "estimated_cost_note": "Nicht verfügbar — kein Kosten-Tracking implementiert.",
        "daily_question_limit": MAX_QUESTIONS_PER_DAY,
        "saved_ai_calls_note": "Regelbasierte Analyse (Scan/Stale/Missing/Changelog) läuft ohne KI-Aufruf.",
    }
