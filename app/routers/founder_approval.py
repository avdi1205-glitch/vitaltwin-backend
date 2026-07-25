"""Smart Approval Center — API (VitalTwin Enterprise, Founder Operating
System, Submodule D).

Mounted at `/api/admin/founder` in `app/main.py` (own file, same prefix as
`founder.py`/`founder_briefing.py`/`founder_tasks.py` — module isolation,
one mount point). Reuses the existing `view_founder_os`/`manage_founder_os`
permissions (deliberately — no new fragmented permission pair; see the
consolidation done right after Release F3).

- `GET /approvals` — runs the detection rules from
  `core/founder_approval_detector.py`, then returns the filtered/searched
  list plus counts. Detection runs synchronously in-request — no
  scheduler, no background job.
- `PATCH /approvals/{id}/status` — Freigeben/Ablehnen/Später prüfen/
  Archivieren. For the two proposal types that represent a real go/no-go
  business decision (`affiliate_product`, `affiliate_partner`), approving
  or rejecting **also** updates the real underlying row (product status /
  partner status) — the founder's click IS the required approval. Every
  other proposal type only changes its own tracking status; the real fix
  happens elsewhere (e.g. re-checking a link in the Affiliate Center).
- `PATCH /approvals/{id}/comment` — Kommentar schreiben.
- `PATCH /approvals/{id}/priority` — Priorität ändern.
- `POST /approvals/bulk` — Massenfreigabe/-ablehnung (Quick Actions "Alle
  freigeben"/"Alle ablehnen").

**Never automatic per spec:** no endpoint here ever changes a price,
publishes a release, changes Premium status, or edits legal text — those
remain explicitly out of reach of this module.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.admin_rbac import require_admin_permission
from ..core.audit import record_audit_event
from ..core.founder_approval_detector import run_detection
from ..core.supabase import supabase
from ..core import automation_engine

router = APIRouter()

APPROVAL_TABLE = "vt_founder_approvals"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
AFFILIATE_PARTNER_TABLE = "vt_affiliate_partners"

ALLOWED_STATUSES = {"neu", "ki_geprueft", "zur_pruefung", "freigegeben", "abgelehnt", "archiviert"}
ALLOWED_PRIORITIES = {"kritisch", "hoch", "mittel", "niedrig"}
DECIDING_STATUSES = {"freigegeben", "abgelehnt"}


class StatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_STATUSES))}")
        return value


class CommentInput(BaseModel):
    comment: str


class PriorityInput(BaseModel):
    priority: str

    @field_validator("priority")
    @classmethod
    def _validate_priority(cls, value: str) -> str:
        if value not in ALLOWED_PRIORITIES:
            raise ValueError(f"Ungültige Priorität. Erlaubt: {', '.join(sorted(ALLOWED_PRIORITIES))}")
        return value


class BulkInput(BaseModel):
    ids: list[str]
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in DECIDING_STATUSES:
            raise ValueError(f"Für Massenaktionen erlaubt: {', '.join(sorted(DECIDING_STATUSES))}")
        return value


def _apply_entity_side_effect(proposal: dict, new_status: str, admin) -> None:
    """Only proposal types that represent a real go/no-go business decision
    with an underlying row to flip get a side effect here — everything
    else is tracking-only.

    `admin` is the full `AdminPrincipal` (not just the email) because the
    Automation Engine (Submodule G) branch below needs to enforce
    "nur Founder oder Super Admin dürfen High-Risk-Regeln aktivieren" —
    a stricter check than the generic `manage_founder_os` permission that
    `automation_manager`/`admin` also hold.
    """
    entity_type = proposal.get("related_entity_type")
    entity_id = proposal.get("related_entity_id")
    admin_email = admin.email if hasattr(admin, "email") else str(admin)
    if not entity_type or not entity_id or new_status not in DECIDING_STATUSES:
        return

    if entity_type == "affiliate_product":
        new_product_status = "approved" if new_status == "freigegeben" else "archived"
        try:
            supabase.table(AFFILIATE_PRODUCT_TABLE).update(
                {"status": new_product_status, "updated_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", entity_id).execute()
        except Exception:
            return
        record_audit_event(
            user_id=None, email=admin_email, action="update", entity_type="affiliate_product", entity_id=entity_id,
            metadata={"status": new_product_status, "via": "founder_approval_center"},
        )
    elif entity_type == "affiliate_partner":
        new_partner_status = "active" if new_status == "freigegeben" else "inactive"
        try:
            supabase.table(AFFILIATE_PARTNER_TABLE).update(
                {"status": new_partner_status, "updated_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", entity_id).execute()
        except Exception:
            return
        record_audit_event(
            user_id=None, email=admin_email, action="update", entity_type="affiliate_partner", entity_id=entity_id,
            metadata={"status": new_partner_status, "via": "founder_approval_center"},
        )
    elif entity_type == "automation_rule":
        # Submodule G — activating a rule is the real go/no-go decision.
        # Medium/High-Risk rule activation is founder/super_admin only,
        # even though `manage_founder_os` alone would otherwise suffice.
        rule = automation_engine.get_rule(entity_id)
        if rule is None:
            return
        if rule.get("risk_level") in ("medium", "high") and getattr(admin, "role", None) != "super_admin":
            return  # silently refuses the side effect; the approval record itself still updates status
        if new_status == "freigegeben":
            automation_engine.set_rule_lifecycle_status(entity_id, status="aktiv", enabled=True, updated_by=admin_email)
        else:
            automation_engine.set_rule_lifecycle_status(entity_id, status="entwurf", enabled=False, updated_by=admin_email)
    elif entity_type == "automation_run":
        # A run that was held for approval (`approval_policy` requiring it).
        run_rows = supabase.table("vt_automation_runs").select("*").eq("id", entity_id).limit(1).execute().data or []
        if not run_rows:
            return
        run = run_rows[0]
        if new_status == "freigegeben":
            rule = automation_engine.get_rule(run.get("rule_id"))
            if rule is not None:
                if rule.get("approval_policy") == "one_time_approval":
                    supabase.table("vt_automation_rules").update({"approved_once": True}).eq("id", rule["id"]).execute()
                automation_engine.execute_rule_run(rule, existing_run=run)
        else:
            supabase.table("vt_automation_runs").update({"status": "abgebrochen", "finished_at": datetime.now(timezone.utc).isoformat()}).eq("id", entity_id).execute()


def _summary(items: list[dict]) -> dict:
    open_statuses = {"neu", "ki_geprueft", "zur_pruefung"}
    return {
        "total": len(items),
        "open": sum(1 for i in items if i.get("status") in open_statuses),
        "critical_open": sum(1 for i in items if i.get("status") in open_statuses and i.get("priority") == "kritisch"),
        "approved": sum(1 for i in items if i.get("status") == "freigegeben"),
        "rejected": sum(1 for i in items if i.get("status") == "abgelehnt"),
        "by_category": _count_by(items, "category"),
    }


def _count_by(items: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = item.get(field) or "unbekannt"
        counts[key] = counts.get(key, 0) + 1
    return counts


@router.get("/approvals")
async def list_approvals(
    category: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    search: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_admin_permission(authorization, "view_founder_os")
    run_detection()

    try:
        items = supabase.table(APPROVAL_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        items = []

    summary = _summary(items)

    if category:
        items = [i for i in items if i.get("category") == category]
    if status:
        items = [i for i in items if i.get("status") == status]
    if priority:
        items = [i for i in items if i.get("priority") == priority]
    if search:
        needle = search.strip().lower()
        items = [
            i
            for i in items
            if needle in str(i.get("title", "")).lower()
            or needle in str(i.get("category", "")).lower()
            or needle in str(i.get("priority", "")).lower()
            or needle in str(i.get("status", "")).lower()
            or needle in str(i.get("created_at", "")).lower()
        ]

    return {"items": items, "summary": summary}


@router.patch("/approvals/{approval_id}/status")
async def update_approval_status(approval_id: str, data: StatusInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    try:
        rows = supabase.table(APPROVAL_TABLE).select("*").eq("id", approval_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden.")
    proposal = rows[0]

    payload = {"status": data.status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if data.status in DECIDING_STATUSES:
        payload["decided_at"] = datetime.now(timezone.utc).isoformat()
        payload["decided_by"] = admin.email

    try:
        supabase.table(APPROVAL_TABLE).update(payload).eq("id", approval_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Status konnte nicht geändert werden.") from exc

    _apply_entity_side_effect(proposal, data.status, admin)
    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="founder_approval", entity_id=approval_id,
        metadata={"status": data.status},
    )
    return {"message": "Status aktualisiert."}


@router.patch("/approvals/{approval_id}/comment")
async def update_approval_comment(approval_id: str, data: CommentInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    try:
        supabase.table(APPROVAL_TABLE).update(
            {"founder_comment": data.comment, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", approval_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Kommentar konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="founder_approval", entity_id=approval_id)
    return {"message": "Kommentar gespeichert."}


@router.patch("/approvals/{approval_id}/priority")
async def update_approval_priority(approval_id: str, data: PriorityInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    try:
        supabase.table(APPROVAL_TABLE).update(
            {"priority": data.priority, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", approval_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Priorität konnte nicht geändert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="founder_approval", entity_id=approval_id)
    return {"message": "Priorität aktualisiert."}


@router.post("/approvals/bulk")
async def bulk_update_approvals(data: BulkInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    updated = 0
    for approval_id in data.ids:
        try:
            rows = supabase.table(APPROVAL_TABLE).select("*").eq("id", approval_id).limit(1).execute().data or []
        except Exception:
            rows = []
        if not rows:
            continue
        proposal = rows[0]
        try:
            supabase.table(APPROVAL_TABLE).update(
                {
                    "status": data.status,
                    "decided_at": datetime.now(timezone.utc).isoformat(),
                    "decided_by": admin.email,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", approval_id).execute()
        except Exception:
            continue
        _apply_entity_side_effect(proposal, data.status, admin)
        updated += 1

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="founder_approval_bulk",
        metadata={"status": data.status, "count": updated},
    )
    return {"updated": updated}
