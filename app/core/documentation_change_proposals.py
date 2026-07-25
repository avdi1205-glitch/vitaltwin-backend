"""Auto Documentation — Change Proposals for Protected Documents
(VitalTwin Enterprise, Founder Operating System, Submodule I).

The **only** path by which a protected document's content is ever
touched: a proposal is prepared here, then handed to the Smart Approval
Center (Submodule D, reused directly — no parallel approval system).
Nothing here ever writes to `generated_content`/`vt_documentation_
versions` for a protected document — approval only records the founder's
decision; applying an approved change to the *real* file remains a manual
step for the founder (this backend has no file-write capability for
frontend docs at all, see `documentation_scanner.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import documentation_protected as protected
from .audit import record_audit_event
from .supabase import supabase

PROPOSAL_TABLE = "vt_documentation_change_proposals"
APPROVAL_TABLE = "vt_founder_approvals"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_change_proposal(*, registry_id: str, document_path: str, proposed_content: str, reason: str, risk_level: str = "hoch", created_by: str) -> dict:
    if not protected.is_protected(document_path):
        raise ValueError("Change Proposals sind ausschließlich für geschützte Dokumente vorgesehen — nicht-geschützte Dokumente werden direkt generiert.")

    payload = {
        "registry_id": registry_id, "proposed_content": proposed_content, "reason": reason,
        "risk_level": risk_level, "status": "offen", "created_by": created_by,
        "diff_summary": {"document_path": document_path},
    }
    response = supabase.table(PROPOSAL_TABLE).insert(payload).execute()
    proposal = response.data[0] if response.data else payload
    record_audit_event(user_id=None, email=created_by, action="create", entity_type="documentation_change_proposal", entity_id=str(proposal.get("id")))
    return proposal


def send_proposal_to_approval_center(proposal_id: str, *, document_path: str, sent_by: str) -> str | None:
    dedupe_key = f"documentation_proposal_{proposal_id}"
    existing = supabase.table(APPROVAL_TABLE).select("id").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing:
        return existing[0]["id"]

    proposal_rows = supabase.table(PROPOSAL_TABLE).select("*").eq("id", proposal_id).limit(1).execute().data or []
    if not proposal_rows:
        return None
    proposal = proposal_rows[0]

    payload = {
        "dedupe_key": dedupe_key, "title": f"Geschütztes Dokument ändern: {document_path}", "category": "dokumentation",
        "source": "auto_documentation", "priority": "hoch", "status": "ki_geprueft", "auto_detected": True,
        "reason": proposal.get("reason", ""), "data_used": document_path,
        "rules_applied": "Geschützte Dokumente dürfen nur nach Freigabe geändert werden.",
        "benefits": "Dokumentation bleibt aktuell.", "risks": "Geschützter Inhalt (rechtlich/strategisch) — Freigabe zwingend erforderlich.",
        "related_entity_type": "documentation_change_proposal", "related_entity_id": str(proposal_id),
    }
    response = supabase.table(APPROVAL_TABLE).insert(payload).execute()
    approval_id = response.data[0]["id"] if response.data else None
    if approval_id:
        supabase.table(PROPOSAL_TABLE).update({"approval_id": approval_id, "updated_at": _now_iso()}).eq("id", proposal_id).execute()
    record_audit_event(user_id=None, email=sent_by, action="create", entity_type="founder_approval", metadata={"via": "auto_documentation", "proposal_id": proposal_id})
    return approval_id
