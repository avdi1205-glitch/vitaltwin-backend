"""Auto Documentation — Documentation Registry (VitalTwin Enterprise,
Founder Operating System, Submodule I).

Central registry (`vt_documentation_registry`) + version history
(`vt_documentation_versions`). Every content change writes a new version
row — this is what makes rollback possible. **Protected documents are
never auto-updated here** (`documentation_protected.py` is checked before
any content write); registering/reviewing their *metadata* (status,
owner, last_reviewed_at) is still allowed, since that alone never changes
their actual wording.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import documentation_protected as protected
from .audit import record_audit_event
from .supabase import supabase

REGISTRY_TABLE = "vt_documentation_registry"
VERSION_TABLE = "vt_documentation_versions"

STATUSES = frozenset({
    "current", "stale", "missing", "draft", "pending_review", "approved",
    "rejected", "archived", "manually_managed",
})

# The Founder-OS documentation this session has built — the one set of
# frontend docs this backend process cannot verify by reading files
# (separate repository), but whose *paths* are a known, stable project
# convention worth seeding once so the registry isn't empty on first use.
KNOWN_FOUNDER_OS_DOCS: tuple[dict, ...] = (
    {"document_path": "frontend/docs/MODULE_MAP.md", "title": "Module Map", "category": "projektuebersicht", "submodule": None},
    {"document_path": "frontend/docs/FOUNDER_DAILY_BRIEFING.md", "title": "Founder Daily Briefing", "category": "founder_os", "submodule": "B"},
    {"document_path": "frontend/docs/AI_FOUNDER_TASK_MANAGER.md", "title": "AI Founder Task Manager", "category": "founder_os", "submodule": "C"},
    {"document_path": "frontend/docs/SMART_APPROVAL_CENTER.md", "title": "Smart Approval Center", "category": "founder_os", "submodule": "D"},
    {"document_path": "frontend/docs/AI_BUSINESS_COACH.md", "title": "AI Business Coach", "category": "founder_os", "submodule": "E"},
    {"document_path": "frontend/docs/AFFILIATE_INTELLIGENCE.md", "title": "Affiliate Intelligence", "category": "affiliate", "submodule": "F"},
    {"document_path": "frontend/docs/AUTOMATION_ENGINE.md", "title": "Automation Engine", "category": "automatisierung", "submodule": "G"},
    {"document_path": "frontend/docs/CEO_INTELLIGENCE.md", "title": "CEO Intelligence", "category": "founder_os", "submodule": "H"},
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_known_documents(*, created_by: str | None = None) -> int:
    """Idempotent: registers each known Founder-OS doc path once, marked
    `manually_managed` + `verified=False` (this backend cannot open these
    files — separate repository) unless already present. Returns the
    number of newly created entries."""
    created = 0
    for doc in KNOWN_FOUNDER_OS_DOCS:
        existing = supabase.table(REGISTRY_TABLE).select("id").eq("document_path", doc["document_path"]).limit(1).execute().data or []
        if existing:
            continue
        supabase.table(REGISTRY_TABLE).insert({
            **doc, "module": "founder_os", "status": "manually_managed", "is_generated": False,
            "requires_approval": False, "protected": protected.is_protected(doc["document_path"]),
            "owner": "founder", "created_by": created_by,
            "stale_reason": "Nicht automatisch prüfbar (separates Frontend-Repository, kein Dateizugriff aus dem Backend-Prozess).",
        }).execute()
        created += 1
    return created


def list_documents(*, category: str | None = None, status: str | None = None, module: str | None = None) -> list[dict]:
    try:
        items = supabase.table(REGISTRY_TABLE).select("*").order("updated_at", desc=True).execute().data or []
    except Exception:
        items = []
    if category:
        items = [i for i in items if i.get("category") == category]
    if status:
        items = [i for i in items if i.get("status") == status]
    if module:
        items = [i for i in items if i.get("module") == module]
    return items


def get_document(registry_id: str) -> dict | None:
    rows = supabase.table(REGISTRY_TABLE).select("*").eq("id", registry_id).limit(1).execute().data or []
    return rows[0] if rows else None


def register_document(payload: dict, *, created_by: str) -> dict:
    row = {**payload, "protected": protected.is_protected(payload.get("document_path", "")), "version": 1, "created_by": created_by}
    response = supabase.table(REGISTRY_TABLE).insert(row).execute()
    saved = response.data[0] if response.data else row
    record_audit_event(user_id=None, email=created_by, action="create", entity_type="documentation_registry", entity_id=str(saved.get("id")))
    return saved


def _write_version(registry_id: str, *, version: int, content: str | None, diff_summary: dict, created_by: str | None) -> None:
    content_hash = None
    if content is not None:
        import hashlib
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        supabase.table(VERSION_TABLE).insert({
            "registry_id": registry_id, "version": version, "content": content,
            "content_hash": content_hash, "diff_summary": diff_summary, "created_by": created_by,
        }).execute()
    except Exception:
        pass


def update_document_content(registry_id: str, *, content: str, diff_summary: dict, updated_by: str | None, source_hash: str | None = None) -> dict:
    """Auto-update path for NON-protected, generated documents only.
    Raises `PermissionError` for protected documents — callers must use
    `propose_change_for_protected` instead."""
    doc = get_document(registry_id)
    if doc is None:
        raise LookupError("Dokument nicht gefunden.")
    protected.assert_not_protected_for_auto_update(doc["document_path"])

    import hashlib
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    new_version = doc.get("version", 1) + 1

    update_payload = {
        "generated_content": content, "content_hash": content_hash, "source_hash": source_hash,
        "version": new_version, "status": "current", "stale_reason": None,
        "last_generated_at": _now_iso(), "updated_at": _now_iso(), "is_generated": True,
    }
    supabase.table(REGISTRY_TABLE).update(update_payload).eq("id", registry_id).execute()
    _write_version(registry_id, version=new_version, content=content, diff_summary=diff_summary, created_by=updated_by)
    record_audit_event(user_id=None, email=updated_by, action="update", entity_type="documentation_registry", entity_id=registry_id, metadata={"event": "auto_generated"})
    return {**doc, **update_payload}


def list_versions(registry_id: str) -> list[dict]:
    return supabase.table(VERSION_TABLE).select("*").eq("registry_id", registry_id).order("version", desc=True).execute().data or []


def rollback_document(registry_id: str, *, target_version: int, rolled_back_by: str) -> dict:
    """Restores a previous documentation *content* version. Never touches
    source files, migrations, or code — content only."""
    doc = get_document(registry_id)
    if doc is None:
        raise LookupError("Dokument nicht gefunden.")
    versions = {v["version"]: v for v in list_versions(registry_id)}
    target = versions.get(target_version)
    if target is None:
        raise LookupError(f"Version {target_version} nicht gefunden.")

    new_version = doc.get("version", 1) + 1
    update_payload = {
        "generated_content": target.get("content"), "content_hash": target.get("content_hash"),
        "version": new_version, "updated_at": _now_iso(),
    }
    supabase.table(REGISTRY_TABLE).update(update_payload).eq("id", registry_id).execute()
    _write_version(registry_id, version=new_version, content=target.get("content"), diff_summary={"rollback_to": target_version}, created_by=rolled_back_by)
    record_audit_event(user_id=None, email=rolled_back_by, action="update", entity_type="documentation_registry", entity_id=registry_id, metadata={"event": "rollback", "target_version": target_version})
    return {**doc, **update_payload}


def mark_reviewed(registry_id: str, *, reviewed_by: str) -> None:
    supabase.table(REGISTRY_TABLE).update({"last_reviewed_at": _now_iso(), "updated_at": _now_iso()}).eq("id", registry_id).execute()
    record_audit_event(user_id=None, email=reviewed_by, action="update", entity_type="documentation_registry", entity_id=registry_id, metadata={"event": "reviewed"})


def archive_document(registry_id: str, *, archived_by: str) -> None:
    """Founder-only per spec — enforced by the router (role check), not
    here — this function itself has no role awareness by design."""
    supabase.table(REGISTRY_TABLE).update({"status": "archived", "updated_at": _now_iso()}).eq("id", registry_id).execute()
    record_audit_event(user_id=None, email=archived_by, action="update", entity_type="documentation_registry", entity_id=registry_id, metadata={"event": "archiviert"})
