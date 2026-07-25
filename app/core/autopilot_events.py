"""Founder Autopilot — Event System (VitalTwin Enterprise, Founder
Operating System, Submodule J).

**No message bus / queue exists in this codebase** (single Railway
process, no Redis/Celery — consistent with every other Founder-OS
module). Events are therefore *synthesized on read* from the real,
already-existing tables of Submodules C/D/F/G/I, then persisted
idempotently (via `dedupe_key`) into `vt_founder_autopilot_events` so the
Today View / Decision Inbox can page through a stable, deduplicated feed
instead of re-scanning raw tables on every render.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .supabase import supabase

EVENT_TABLE = "vt_founder_autopilot_events"

SEVERITY_ORDER = {"kritisch": 4, "hoch": 3, "mittel": 2, "niedrig": 1, "information": 0}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_event(*, event_type: str, source_module: str, source_id: str | None, severity: str, payload_reference: str | None, dedupe_key: str) -> None:
    existing = supabase.table(EVENT_TABLE).select("id,status").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing:
        if existing[0].get("status") in ("erledigt", "archiviert"):
            return
        supabase.table(EVENT_TABLE).update({"severity": severity, "occurred_at": _now_iso()}).eq("dedupe_key", dedupe_key).execute()
        return
    try:
        supabase.table(EVENT_TABLE).insert({
            "event_type": event_type, "source_module": source_module, "source_id": source_id,
            "severity": severity, "payload_reference": payload_reference, "dedupe_key": dedupe_key, "status": "offen",
        }).execute()
    except Exception:
        pass


def collect_events_from_modules() -> dict:
    """Runs all real, read-only collectors once. Returns a summary count
    per event type. Never raises — a module whose table doesn't exist yet
    (migration not run) is simply skipped for this cycle."""
    collected = 0

    try:
        tasks = supabase.table("vt_founder_tasks").select("id,status,priority,created_at").execute().data or []
        for task in tasks:
            if task.get("status") in ("neu", "in_bearbeitung", "warten"):
                severity = "hoch" if task.get("priority") in ("kritisch", "hoch") else "mittel"
                _upsert_event(event_type="task.created", source_module="C", source_id=task["id"], severity=severity,
                               payload_reference="vt_founder_tasks", dedupe_key=f"task_created_{task['id']}")
                collected += 1
    except Exception:
        pass

    try:
        approvals = supabase.table("vt_founder_approvals").select("id,status,priority,category").execute().data or []
        for approval in approvals:
            if approval.get("status") in ("neu", "ki_geprueft", "zur_pruefung"):
                severity = "kritisch" if approval.get("priority") == "kritisch" else "hoch" if approval.get("priority") == "hoch" else "mittel"
                _upsert_event(event_type="approval.requested", source_module="D", source_id=approval["id"], severity=severity,
                               payload_reference="vt_founder_approvals", dedupe_key=f"approval_requested_{approval['id']}")
                collected += 1
    except Exception:
        pass

    try:
        products = supabase.table("vt_affiliate_products").select("id,link_status").execute().data or []
        broken = [p for p in products if p.get("link_status") == "broken"]
        if broken:
            today = date.today().isoformat()
            _upsert_event(event_type="affiliate.link_broken", source_module="F", source_id=None, severity="mittel",
                           payload_reference=f"{len(broken)} defekte Links", dedupe_key=f"affiliate_link_broken_{today}")
            collected += 1
    except Exception:
        pass

    try:
        runs = supabase.table("vt_automation_runs").select("id,status,rule_id,created_at").gte(
            "created_at", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()).execute().data or []
        for run in runs:
            if run.get("status") in ("fehlgeschlagen", "dead_letter"):
                _upsert_event(event_type="automation.failed", source_module="G", source_id=run["id"], severity="hoch",
                               payload_reference="vt_automation_runs", dedupe_key=f"automation_failed_{run['id']}")
                collected += 1
    except Exception:
        pass

    try:
        docs = supabase.table("vt_documentation_registry").select("id,status,document_path").execute().data or []
        stale_docs = [d for d in docs if d.get("status") == "stale"]
        if stale_docs:
            today = date.today().isoformat()
            _upsert_event(event_type="documentation.stale", source_module="I", source_id=None, severity="niedrig",
                           payload_reference=f"{len(stale_docs)} veraltete Dokumente", dedupe_key=f"documentation_stale_{today}")
            collected += 1
    except Exception:
        pass

    return {"collected": collected, "computed_at": _now_iso()}


def list_events(*, status: str | None = None, severity: str | None = None) -> list[dict]:
    items = supabase.table(EVENT_TABLE).select("*").order("occurred_at", desc=True).limit(200).execute().data or []
    if status:
        items = [i for i in items if i.get("status") == status]
    if severity:
        items = [i for i in items if i.get("severity") == severity]
    return sorted(items, key=lambda i: SEVERITY_ORDER.get(i.get("severity"), 0), reverse=True)


def mark_event_handled(event_id: str) -> None:
    supabase.table(EVENT_TABLE).update({"status": "erledigt", "handled_at": _now_iso()}).eq("id", event_id).execute()
