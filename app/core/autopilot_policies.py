"""Founder Autopilot — Policies (VitalTwin Enterprise, Founder Operating
System, Submodule J).

A Policy describes *when* Founder Autopilot is allowed to let Automation
Engine (Submodule G) execute something on its own vs. when a founder
decision is mandatory. Policies never grant `critical` risk or any
category from `ALWAYS_MANUAL_CATEGORIES` — validated the same way
Submodule G's Safe Action Registry validates rules.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import automation_registry as g_registry
from .audit import record_audit_event
from .supabase import supabase

POLICY_TABLE = "vt_founder_autopilot_policies"

# Per spec's "Always Manual" list — these are never eligible for any
# Autopilot policy's `allowed_categories`, regardless of risk level.
ALWAYS_MANUAL_CATEGORIES: frozenset[str] = frozenset({
    "preise", "tarife", "budgets", "vertraege", "rechtliches", "datenschutz",
    "sicherheitsrichtlinien", "api_schluessel", "releases_produktiv", "branding", "strategie",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_policy_payload(payload: dict) -> None:
    if payload.get("maximum_risk_level") == "critical":
        raise ValueError("Policies dürfen niemals 'critical' als maximumRiskLevel erlauben.")
    allowed = set(payload.get("allowed_categories") or [])
    if allowed & ALWAYS_MANUAL_CATEGORIES:
        raise ValueError(f"Folgende Kategorien sind immer manuell und dürfen nie erlaubt werden: {', '.join(sorted(allowed & ALWAYS_MANUAL_CATEGORIES))}")
    if payload.get("mode") not in ("off", "monitor", "assist", "controlled_autopilot", "maintenance", "incident_mode"):
        raise ValueError("Ungültiger Modus für Policy.")


def create_policy(payload: dict, *, created_by: str) -> dict:
    validate_policy_payload(payload)
    row = {**payload, "version": 1, "previous_versions": [], "status": "entwurf", "enabled": False, "created_by": created_by}
    response = supabase.table(POLICY_TABLE).insert(row).execute()
    saved = response.data[0] if response.data else row
    record_audit_event(user_id=None, email=created_by, action="create", entity_type="autopilot_policy", entity_id=str(saved.get("id")))
    return saved


def update_policy(policy_id: str, payload: dict, *, updated_by: str) -> dict:
    existing_rows = supabase.table(POLICY_TABLE).select("*").eq("id", policy_id).limit(1).execute().data or []
    if not existing_rows:
        raise LookupError("Policy nicht gefunden.")
    existing = existing_rows[0]
    merged = {**existing, **payload}
    validate_policy_payload(merged)

    previous_versions = list(existing.get("previous_versions") or [])
    previous_versions.append({"version": existing["version"], "snapshot": {k: v for k, v in existing.items() if k not in ("previous_versions",)}})

    update_payload = {k: v for k, v in payload.items() if k not in ("id", "created_at", "created_by", "version", "previous_versions")}
    update_payload["version"] = existing["version"] + 1
    update_payload["previous_versions"] = previous_versions
    update_payload["updated_at"] = _now_iso()
    # Any change requires re-review before it can run again.
    update_payload.setdefault("status", "entwurf")
    update_payload["status"] = "entwurf"
    update_payload["enabled"] = False

    supabase.table(POLICY_TABLE).update(update_payload).eq("id", policy_id).execute()
    record_audit_event(user_id=None, email=updated_by, action="update", entity_type="autopilot_policy", entity_id=policy_id)
    return {**existing, **update_payload}


def list_policies() -> list[dict]:
    return supabase.table(POLICY_TABLE).select("*").order("created_at", desc=True).execute().data or []


def get_policy(policy_id: str) -> dict | None:
    rows = supabase.table(POLICY_TABLE).select("*").eq("id", policy_id).limit(1).execute().data or []
    return rows[0] if rows else None


def activate_policy(policy_id: str, *, activated_by: str) -> dict:
    """Activating a `controlled_autopilot`-mode policy is founder/
    super_admin-only — enforced in the router, not here."""
    supabase.table(POLICY_TABLE).update({"status": "aktiv", "enabled": True, "updated_at": _now_iso()}).eq("id", policy_id).execute()
    record_audit_event(user_id=None, email=activated_by, action="update", entity_type="autopilot_policy", entity_id=policy_id, metadata={"event": "aktiviert"})
    return {"id": policy_id, "status": "aktiv", "enabled": True}


def pause_policy(policy_id: str, *, paused_by: str) -> dict:
    supabase.table(POLICY_TABLE).update({"status": "pausiert", "enabled": False, "updated_at": _now_iso()}).eq("id", policy_id).execute()
    record_audit_event(user_id=None, email=paused_by, action="update", entity_type="autopilot_policy", entity_id=policy_id, metadata={"event": "pausiert"})
    return {"id": policy_id, "status": "pausiert", "enabled": False}


def effective_allowed_categories(policies: list[dict] | None = None) -> frozenset[str]:
    """Union of all enabled, active policies' `allowed_categories` — used
    by the orchestrator to further narrow whatever the current Autopilot
    Mode already allows (mode AND policy, never mode OR policy)."""
    if policies is None:
        policies = list_policies()
    categories: set[str] = set()
    for policy in policies:
        if policy.get("enabled") and policy.get("status") == "aktiv":
            categories.update(policy.get("allowed_categories") or [])
    return frozenset(categories) & g_registry.CATEGORIES_WITH_REAL_ACTIONS
