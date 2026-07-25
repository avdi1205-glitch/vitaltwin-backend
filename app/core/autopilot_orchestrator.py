"""Founder Autopilot — Central Orchestrator (VitalTwin Enterprise, Founder
Operating System, Submodule J).

The single place that ties Submodules A-I together for the founder-
facing "Today View" / "Decision Inbox" / One-Click Approval / daily
orchestration cycle. **Never a second automation engine** — the
orchestrator only decides *whether* `core/automation_engine.py`
(Submodule G) is allowed to run, via `core/autopilot_state.py` (mode +
kill switch) and `core/autopilot_policies.py` (category allowlist); the
actual execution, retry, rollback, and Safe Action Registry enforcement
all stay in G, unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import automation_engine as g_engine
from . import autopilot_alerts as alerts_module
from . import autopilot_events as events_module
from . import autopilot_policies as policies_module
from . import autopilot_priority as priority_module
from . import autopilot_state as state_module
from .audit import record_audit_event
from .concurrency import run_parallel
from .supabase import supabase

APPROVAL_TABLE = "vt_founder_approvals"
TASK_TABLE = "vt_founder_tasks"

# Categories that may never be bulk-approved in one click, per spec
# ("keine sensiblen Produkte, keine rechtlichen Inhalte, keine Preis-
# /Tarifänderungen, keine kritischen Systemeinstellungen").
BULK_APPROVAL_EXCLUDED_CATEGORIES: frozenset[str] = frozenset({
    "rechtliches", "datenschutz", "preise", "tarife", "sicherheit", "budgets", "vertraege",
})
BULK_APPROVAL_ALLOWED_PRIORITIES: frozenset[str] = frozenset({"mittel", "niedrig"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_today_view() -> dict:
    def _runs_today() -> tuple[int | None, int | None]:
        try:
            runs_today = supabase.table("vt_automation_runs").select("status,created_at").execute().data or []
            today_start = datetime.now(timezone.utc).date().isoformat()
            runs_today = [r for r in runs_today if str(r.get("created_at", "")).startswith(today_start)]
            return (
                sum(1 for r in runs_today if r.get("status") == "erfolgreich"),
                sum(1 for r in runs_today if r.get("status") in ("fehlgeschlagen", "dead_letter")),
            )
        except Exception:
            return None, None

    def _waiting_approvals() -> int | None:
        try:
            return len([a for a in (supabase.table(APPROVAL_TABLE).select("status").execute().data or []) if a.get("status") in ("neu", "ki_geprueft", "zur_pruefung")])
        except Exception:
            return None

    # These 4 are fully independent — run them concurrently. `list_events`
    # further below must stay sequential, since it needs to see the rows
    # `collect_events_from_modules` just wrote.
    _, _, (auto_completed_today, failed_today), waiting_approvals = run_parallel(
        events_module.collect_events_from_modules,
        alerts_module.run_alert_detection,
        _runs_today,
        _waiting_approvals,
    )

    events = events_module.list_events(status="offen")
    entries = []
    for event in events[:30]:
        entries.append({
            "source": event.get("source_module"), "priority": priority_module.compute_priority(
                {"severity": event.get("severity"), "category": event.get("event_type")}
            ),
            "status": event.get("status"), "occurred_at": event.get("occurred_at"),
            "reason": event.get("event_type"), "next_action": event.get("payload_reference"),
            "approval_required": event.get("event_type") == "approval.requested",
        })

    return {
        "computed_at": _now_iso(),
        "auto_completed_today": auto_completed_today,
        "failed_automations_today": failed_today,
        "waiting_approvals": waiting_approvals,
        "entries": entries,
        "autopilot_state": state_module.get_current_state(),
    }


def get_decision_inbox() -> list[dict]:
    try:
        approvals = supabase.table(APPROVAL_TABLE).select("*").execute().data or []
    except Exception:
        approvals = []
    open_statuses = {"neu", "ki_geprueft", "zur_pruefung"}
    open_approvals = [a for a in approvals if a.get("status") in open_statuses]

    enriched = []
    for approval in open_approvals:
        attention = priority_module.compute_attention_score({
            "severity": approval.get("priority"), "category": approval.get("category"),
            "reversible": approval.get("category") not in ("preise", "tarife", "rechtliches"),
            "requires_approval": True,
        })
        enriched.append({**approval, "attention_score": attention})

    return sorted(enriched, key=lambda a: a["attention_score"], reverse=True)


def _validate_bulk_eligibility(approvals: list[dict]) -> None:
    if not approvals:
        raise ValueError("Keine Freigaben ausgewählt.")
    categories = {a.get("category") for a in approvals}
    priorities = {a.get("priority") for a in approvals}
    if len(categories) > 1:
        raise ValueError("Sammelfreigabe erfordert dieselbe Kategorie für alle ausgewählten Elemente.")
    if len(priorities) > 1:
        raise ValueError("Sammelfreigabe erfordert dasselbe Risikoniveau (Priorität) für alle ausgewählten Elemente.")
    category = next(iter(categories))
    priority = next(iter(priorities))
    if category in BULK_APPROVAL_EXCLUDED_CATEGORIES:
        raise ValueError(f"Kategorie '{category}' ist von Sammelfreigaben ausgeschlossen.")
    if priority not in BULK_APPROVAL_ALLOWED_PRIORITIES:
        raise ValueError("Nur Freigaben mit Priorität 'mittel' oder 'niedrig' sind für Sammelfreigaben zulässig.")
    for approval in approvals:
        if approval.get("related_entity_type") == "affiliate_partner":
            raise ValueError("Partnerprogramm-Aktivierungen sind von Sammelfreigaben ausgeschlossen (immer manuell).")


def execute_one_click_approval(approval_ids: list[str], *, decided_by: str) -> dict:
    """Safe bulk approval — validated against the same exclusion rules as
    the spec's "One-Click Approval" section. Applies the identical real
    side effects the Smart Approval Center itself would (affiliate
    product approval, automation rule activation) — small, local,
    intentionally scoped reimplementation (see docs/FOUNDER_AUTOPILOT.md
    for the reuse-vs-duplication rationale), never a parallel approval
    system."""
    rows = supabase.table(APPROVAL_TABLE).select("*").in_("id", approval_ids).execute().data or []
    _validate_bulk_eligibility(rows)

    updated = 0
    for approval in rows:
        supabase.table(APPROVAL_TABLE).update({
            "status": "freigegeben", "decided_at": _now_iso(), "decided_by": decided_by, "updated_at": _now_iso(),
        }).eq("id", approval["id"]).execute()

        entity_type = approval.get("related_entity_type")
        entity_id = approval.get("related_entity_id")
        if entity_type == "affiliate_product" and entity_id:
            supabase.table("vt_affiliate_products").update({"status": "approved", "updated_at": _now_iso()}).eq("id", entity_id).execute()
        elif entity_type == "automation_rule" and entity_id:
            rule = g_engine.get_rule(entity_id)
            if rule and rule.get("risk_level") == "low":
                g_engine.set_rule_lifecycle_status(entity_id, status="aktiv", enabled=True, updated_by=decided_by)
        updated += 1

    record_audit_event(user_id=None, email=decided_by, action="update", entity_type="autopilot_one_click_approval", metadata={"count": updated})
    return {"updated": updated}


def run_orchestration_cycle(*, triggered_by: str | None = None) -> dict:
    """The one "on-read" orchestration tick — mirrors Submodule G's
    `evaluate_and_run_due_rules()` entrypoint but adds Autopilot's own
    mode/kill-switch/policy gate in front of it."""
    state = state_module.get_current_state()

    if state.get("kill_switch_active"):
        return {"status": "gestoppt", "reason": "Kill Switch ist aktiv.", "executed": False}

    mode = state.get("mode", state_module.DEFAULT_PRODUCTION_MODE)
    events_module.collect_events_from_modules()
    alerts_module.run_alert_detection()

    if mode in ("off", "monitor"):
        return {"status": "nur_beobachtet", "mode": mode, "executed": False}

    if mode == "assist":
        # Prepare/detect only — never auto-execute, approval remains required.
        return {"status": "vorbereitet", "mode": mode, "executed": False}

    allowed_categories = state_module.allowed_categories_for_current_state()
    policy_categories = policies_module.effective_allowed_categories()
    effective_categories = allowed_categories & policy_categories if allowed_categories else frozenset()

    if not effective_categories:
        return {"status": "keine_erlaubten_kategorien", "mode": mode, "executed": False}

    try:
        rules = supabase.table("vt_automation_rules").select("*").eq("enabled", True).eq("status", "aktiv").execute().data or []
    except Exception:
        rules = []
    eligible_rule_ids = [r["id"] for r in rules if r.get("category") in effective_categories]

    result = g_engine.evaluate_and_run_due_rules()
    record_audit_event(user_id=None, email=triggered_by, action="update", entity_type="autopilot_orchestration_cycle", metadata={"mode": mode, "eligible_rules": len(eligible_rule_ids)})
    return {"status": "ausgefuehrt", "mode": mode, "executed": True, "eligible_rule_count": len(eligible_rule_ids), "automation_engine_result": result}
