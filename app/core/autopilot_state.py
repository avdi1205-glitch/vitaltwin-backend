"""Founder Autopilot — Mode, Kill Switch & Incident Mode (VitalTwin
Enterprise, Founder Operating System, Submodule J).

**No parallel automation engine.** This module never executes anything
itself — it only decides *whether* `core/automation_engine.py::
evaluate_and_run_due_rules()` (Submodule G) is allowed to run at all, and
under which category restrictions. All actual execution stays in G.

Every mode/kill-switch/incident change is auditable via
`vt_founder_autopilot_state` (append-only — the current state is always
the most recent row) and `vt_founder_autopilot_kill_switch_events`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from .audit import record_audit_event
from .supabase import supabase

STATE_TABLE = "vt_founder_autopilot_state"
KILL_SWITCH_EVENT_TABLE = "vt_founder_autopilot_kill_switch_events"
INCIDENT_TABLE = "vt_founder_autopilot_incidents"

AutopilotMode = Literal["off", "monitor", "assist", "controlled_autopilot", "maintenance", "incident_mode"]
MODES: frozenset[str] = frozenset({"off", "monitor", "assist", "controlled_autopilot", "maintenance", "incident_mode"})
DEFAULT_PRODUCTION_MODE = "assist"

# Categories a rule must belong to for it to be allowed to auto-execute
# under CONTROLLED_AUTOPILOT — matches the spec's "Safe Autopilot
# Actions" list, mapped onto Automation Engine's existing categories
# (Submodule G) rather than inventing a second category system.
CONTROLLED_AUTOPILOT_ALLOWED_CATEGORIES: frozenset[str] = frozenset({
    "affiliate", "business", "analytics", "reports", "system_monitoring",
    "api_monitoring", "founder_tasks", "founder_briefing", "dokumentation",
})
MAINTENANCE_ALLOWED_CATEGORIES: frozenset[str] = frozenset({"system_monitoring", "api_monitoring", "backups", "tests"})
INCIDENT_MODE_ALLOWED_CATEGORIES: frozenset[str] = frozenset({"system_monitoring", "api_monitoring"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_current_state() -> dict:
    rows = supabase.table(STATE_TABLE).select("*").order("created_at", desc=True).limit(1).execute().data or []
    if rows:
        return rows[0]
    return {"mode": DEFAULT_PRODUCTION_MODE, "kill_switch_active": False, "incident_mode_active": False, "reason": None}


def set_mode(mode: str, *, reason: str | None, changed_by: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"Ungültiger Modus. Erlaubt: {', '.join(sorted(MODES))}")
    current = get_current_state()
    payload = {
        "mode": mode, "kill_switch_active": current.get("kill_switch_active", False),
        "incident_mode_active": mode == "incident_mode", "reason": reason, "created_by": changed_by,
    }
    response = supabase.table(STATE_TABLE).insert(payload).execute()
    record_audit_event(user_id=None, email=changed_by, action="update", entity_type="autopilot_state", metadata={"event": "modus_geaendert", "mode": mode})
    return response.data[0] if response.data else payload


def activate_kill_switch(*, reason: str, activated_by: str) -> dict:
    current = get_current_state()
    payload = {
        "mode": current.get("mode", DEFAULT_PRODUCTION_MODE), "kill_switch_active": True,
        "incident_mode_active": current.get("incident_mode_active", False), "reason": reason, "created_by": activated_by,
    }
    response = supabase.table(STATE_TABLE).insert(payload).execute()
    supabase.table(KILL_SWITCH_EVENT_TABLE).insert({"action": "activated", "reason": reason, "performed_by": activated_by}).execute()
    record_audit_event(user_id=None, email=activated_by, action="update", entity_type="autopilot_kill_switch", metadata={"event": "aktiviert", "reason": reason})
    return response.data[0] if response.data else payload


def deactivate_kill_switch(*, deactivated_by: str) -> dict:
    current = get_current_state()
    payload = {
        "mode": current.get("mode", DEFAULT_PRODUCTION_MODE), "kill_switch_active": False,
        "incident_mode_active": current.get("incident_mode_active", False), "reason": None, "created_by": deactivated_by,
    }
    response = supabase.table(STATE_TABLE).insert(payload).execute()
    supabase.table(KILL_SWITCH_EVENT_TABLE).insert({"action": "deactivated", "reason": None, "performed_by": deactivated_by}).execute()
    record_audit_event(user_id=None, email=deactivated_by, action="update", entity_type="autopilot_kill_switch", metadata={"event": "aufgehoben"})
    return response.data[0] if response.data else payload


def activate_incident_mode(*, title: str, reason: str, activated_by: str) -> dict:
    incident_payload = {"title": title, "reason": reason, "status": "aktiv", "activated_by": activated_by}
    response = supabase.table(INCIDENT_TABLE).insert(incident_payload).execute()
    set_mode("incident_mode", reason=reason, changed_by=activated_by)
    record_audit_event(user_id=None, email=activated_by, action="update", entity_type="autopilot_incident", metadata={"event": "aktiviert", "title": title})
    return response.data[0] if response.data else incident_payload


def resolve_incident(incident_id: str, *, resolved_by: str) -> dict:
    supabase.table(INCIDENT_TABLE).update(
        {"status": "geloest", "resolved_at": _now_iso(), "resolved_by": resolved_by, "updated_at": _now_iso()}
    ).eq("id", incident_id).execute()
    set_mode(DEFAULT_PRODUCTION_MODE, reason="Incident behoben.", changed_by=resolved_by)
    record_audit_event(user_id=None, email=resolved_by, action="update", entity_type="autopilot_incident", entity_id=incident_id, metadata={"event": "geloest"})
    return {"id": incident_id, "status": "geloest"}


def list_incidents() -> list[dict]:
    return supabase.table(INCIDENT_TABLE).select("*").order("created_at", desc=True).execute().data or []


def allowed_categories_for_current_state() -> frozenset[str] | None:
    """Returns the category allowlist for automatic execution given the
    current mode, or `None` if all categories are allowed (only true for
    `controlled_autopilot` with no active restriction, still gated by
    Automation Engine's own approval policies)."""
    state = get_current_state()
    if state.get("kill_switch_active"):
        return frozenset()  # nothing at all
    mode = state.get("mode", DEFAULT_PRODUCTION_MODE)
    if mode == "off":
        return frozenset()
    if mode == "monitor":
        return frozenset()  # observe/detect only, never execute
    if mode == "assist":
        return frozenset()  # prepares only — everything needs explicit approval
    if mode == "maintenance":
        return MAINTENANCE_ALLOWED_CATEGORIES
    if mode == "incident_mode":
        return INCIDENT_MODE_ALLOWED_CATEGORIES
    if mode == "controlled_autopilot":
        return CONTROLLED_AUTOPILOT_ALLOWED_CATEGORIES
    return frozenset()
