"""Automation Engine — core rule engine (VitalTwin Enterprise, Founder
Operating System, Submodule G).

**Module boundary (per spec):** this file belongs exclusively to the
Founder Operating System. It never reads or writes health, CGM,
nutrition, sleep, movement, biomarker, or Twin Memory data — only
Founder-OS data, aggregated business data, system/integration status,
and the automation rules/runs it manages itself.

**No background scheduler exists in this codebase** (single Railway
process, no Celery/Redis queue — see
`frontend/docs/PLATFORM_ARCHITECTURE.md`). Exactly like every other
Founder-OS module ("no cron, computed on read"), rule evaluation happens
synchronously inside a request:

- `GET .../automation/dashboard` calls `evaluate_and_run_due_rules()`
  once before returning, so opening the dashboard is enough to keep
  schedule-based rules moving.
- `POST .../automation/run-due` does the same and can be wired to an
  external cron caller (e.g. a free GitHub Actions schedule hitting this
  endpoint) for founders who want closer-to-real-time execution — this
  repo does not ship such a caller itself, that remains a manual/founder
  setup step.

**Idempotency.** Every run is keyed by a stable `idempotency_key`
(`f"{rule_id}:{trigger_signature}"`). Re-evaluating the same rule with an
unchanged trigger signature (e.g. same calendar day for a daily schedule)
never creates a second run or re-executes already-succeeded actions.

**Retry, without a background worker.** A failed run whose `attempt <
max_attempts` (per the rule's `retry_policy`) is left in status
`fehlgeschlagen_wird_wiederholt`; the *next* time `evaluate_and_run_due_rules`
runs (next dashboard load / next `run-due` call) and the cooldown window
has elapsed, the SAME run row is retried (attempt incremented) rather
than a new one being created. After `max_attempts` is reached, the run
moves to `dead_letter`, a Task Manager task and a Founder Alert are
created, and an audit event is written — no endless retries are possible.

**Rollback.** Only actions marked `reversible=True` in the Safe Action
Registry ever populate `previous_state` on their run; only those runs can
be rolled back, and only once.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from . import automation_registry as registry
from .affiliate_link_checker import check_link
from .affiliate_provider import get_provider_statuses
from .audit import record_audit_event
from .automation_conditions import evaluate_conditions
from .founder_business_metrics import get_business_dashboard
from .integrations import get_full_integration_report
from .supabase import supabase

RULE_TABLE = "vt_automation_rules"
RULE_VERSION_TABLE = "vt_automation_rule_versions"
RUN_TABLE = "vt_automation_runs"
DEAD_LETTER_TABLE = "vt_automation_dead_letters"
ALERT_TABLE = "vt_automation_alerts"
TASK_TABLE = "vt_founder_tasks"
APPROVAL_TABLE = "vt_founder_approvals"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"

RULE_STATUSES = {"entwurf", "wartet_auf_freigabe", "aktiv", "pausiert", "archiviert"}
RUN_STATUSES = {
    "wartend", "laeuft", "erfolgreich", "teilweise_erfolgreich",
    "fehlgeschlagen_wird_wiederholt", "fehlgeschlagen", "zurueckgerollt",
    "wartet_auf_freigabe", "timeout", "abgebrochen", "dead_letter",
}
TERMINAL_RUN_STATUSES = {"erfolgreich", "fehlgeschlagen", "zurueckgerollt", "abgebrochen", "dead_letter"}
APPROVAL_REQUIRED_RISKS = {"medium", "high"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _canonical_signature(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def build_idempotency_key(rule_id: str, trigger_signature: str) -> str:
    return f"{rule_id}:{trigger_signature}"


def _rule_default_next_run_at(trigger_config: dict) -> str | None:
    interval_hours = trigger_config.get("interval_hours")
    if not isinstance(interval_hours, (int, float)) or interval_hours <= 0:
        return None
    return (_now() + timedelta(hours=interval_hours)).isoformat()


# ---------------------------------------------------------------------------
# Rule CRUD + versioning
# ---------------------------------------------------------------------------


def _snapshot_rule(rule: dict) -> dict:
    return {k: v for k, v in rule.items() if k not in {"created_at", "updated_at"}}


def _write_version(rule: dict, *, created_by: str | None) -> None:
    try:
        supabase.table(RULE_VERSION_TABLE).insert(
            {
                "rule_id": rule["id"],
                "version": rule["version"],
                "snapshot": _snapshot_rule(rule),
                "created_by": created_by,
            }
        ).execute()
    except Exception:
        pass


def create_rule(payload: dict, *, created_by: str) -> dict:
    """Validates against the Safe Action Registry (rejects unknown/
    unimplemented/critical-risk actions outright), then persists version 1.
    New rules are always created disabled — the founder must explicitly
    activate them (`request_rule_activation`), and in `production` this is
    non-negotiable regardless of what the caller passes for `enabled`."""
    environment = payload.get("environment", "production")
    registry.validate_risk_level(payload.get("risk_level", ""))
    registry.validate_approval_policy(payload.get("approval_policy", "no_approval"))
    if payload.get("trigger_type") not in registry.TRIGGER_TYPES:
        raise ValueError(f"Ungültiger trigger_type. Erlaubt: {', '.join(sorted(registry.TRIGGER_TYPES))}")
    if payload.get("category") not in registry.AUTOMATION_CATEGORIES:
        raise ValueError(f"Ungültige Kategorie. Erlaubt: {', '.join(sorted(registry.AUTOMATION_CATEGORIES))}")
    registry.validate_actions(payload.get("actions", []), environment=environment)

    row = {
        **payload,
        "enabled": False,
        "status": "entwurf",
        "version": 1,
        "run_count": 0,
        "approved_once": False,
        "created_by": created_by,
        "next_run_at": _rule_default_next_run_at(payload.get("trigger_config", {})) if payload.get("trigger_type") == "schedule" else None,
    }
    try:
        response = supabase.table(RULE_TABLE).insert(row).execute()
    except Exception as exc:
        raise RuntimeError("Regel konnte nicht gespeichert werden.") from exc
    saved = response.data[0] if response.data else row
    _write_version(saved, created_by=created_by)
    record_audit_event(user_id=None, email=created_by, action="create", entity_type="automation_rule", entity_id=str(saved.get("id")))
    return saved


def update_rule(rule_id: str, payload: dict, *, updated_by: str) -> dict:
    existing_rows = supabase.table(RULE_TABLE).select("*").eq("id", rule_id).limit(1).execute().data or []
    if not existing_rows:
        raise LookupError("Regel nicht gefunden.")
    existing = existing_rows[0]

    merged = {**existing, **payload}
    environment = merged.get("environment", "production")
    registry.validate_risk_level(merged.get("risk_level", ""))
    registry.validate_approval_policy(merged.get("approval_policy", "no_approval"))
    registry.validate_actions(merged.get("actions", []), environment=environment)

    update_payload = {
        k: v for k, v in payload.items()
        if k not in {"id", "created_at", "created_by", "version", "run_count"}
    }
    update_payload["version"] = existing["version"] + 1
    update_payload["updated_at"] = _now_iso()
    # Any change to a rule's risk-relevant configuration means it must be
    # re-approved before running again if it carries medium/high risk —
    # never silently keep an old approval valid for new conditions/actions.
    if merged.get("risk_level") in APPROVAL_REQUIRED_RISKS:
        update_payload["status"] = "entwurf"
        update_payload["enabled"] = False
        update_payload["approved_once"] = False

    try:
        supabase.table(RULE_TABLE).update(update_payload).eq("id", rule_id).execute()
    except Exception as exc:
        raise RuntimeError("Regel konnte nicht aktualisiert werden.") from exc

    saved = {**existing, **update_payload}
    _write_version(saved, created_by=updated_by)
    record_audit_event(user_id=None, email=updated_by, action="update", entity_type="automation_rule", entity_id=rule_id)
    return saved


def get_rule(rule_id: str) -> dict | None:
    rows = supabase.table(RULE_TABLE).select("*").eq("id", rule_id).limit(1).execute().data or []
    return rows[0] if rows else None


def list_rule_versions(rule_id: str) -> list[dict]:
    return supabase.table(RULE_VERSION_TABLE).select("*").eq("rule_id", rule_id).order("version", desc=True).execute().data or []


def set_rule_lifecycle_status(rule_id: str, *, status: str, enabled: bool, updated_by: str) -> dict:
    if status not in RULE_STATUSES:
        raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(RULE_STATUSES))}")
    supabase.table(RULE_TABLE).update(
        {"status": status, "enabled": enabled, "updated_at": _now_iso()}
    ).eq("id", rule_id).execute()
    record_audit_event(
        user_id=None, email=updated_by, action="update", entity_type="automation_rule", entity_id=rule_id,
        metadata={"lifecycle_status": status, "enabled": enabled},
    )
    return {"id": rule_id, "status": status, "enabled": enabled}


def request_rule_activation(rule_id: str, *, requested_by: str) -> dict:
    """Low risk → activates immediately. Medium/High risk → creates a
    Smart Approval Center request and puts the rule into
    `wartet_auf_freigabe`; the actual activation happens as a side effect
    of the founder approving it in `routers/founder_approval.py`
    (`related_entity_type='automation_rule'`) — reused, not duplicated."""
    rule = get_rule(rule_id)
    if rule is None:
        raise LookupError("Regel nicht gefunden.")

    risk_level = rule.get("risk_level")
    if risk_level == "low" and rule.get("approval_policy") == "no_approval":
        return set_rule_lifecycle_status(rule_id, status="aktiv", enabled=True, updated_by=requested_by)

    if risk_level in APPROVAL_REQUIRED_RISKS or rule.get("approval_policy") != "no_approval":
        _create_or_refresh_approval(
            dedupe_key=f"automation_rule_activation_{rule_id}_v{rule.get('version')}",
            title=f"Automatisierungsregel aktivieren: {rule.get('name')}",
            category="automation",
            source="automation_engine",
            reason=(
                f"Regel '{rule.get('name')}' (Risk Level: {risk_level}) soll aktiviert werden. "
                f"Trigger: {rule.get('trigger_type')}. Aktionen: "
                f"{', '.join(a.get('action_type', '?') for a in rule.get('actions', []))}."
            ),
            data_used=f"vt_automation_rules.id={rule_id}, version={rule.get('version')}",
            rules_applied="Regel: risk_level in (medium, high) ODER approval_policy != no_approval → Freigabe erforderlich.",
            benefits="Wiederkehrende operative Aufgabe wird nach Freigabe automatisch erledigt.",
            risks=f"Aktionen mit Risk Level '{risk_level}' können reale Daten verändern (siehe Regel-Aktionen).",
            related_entity_type="automation_rule",
            related_entity_id=str(rule_id),
        )
        return set_rule_lifecycle_status(rule_id, status="wartet_auf_freigabe", enabled=False, updated_by=requested_by)

    return set_rule_lifecycle_status(rule_id, status="aktiv", enabled=True, updated_by=requested_by)


# ---------------------------------------------------------------------------
# Approval Center integration (shared upsert helper, same shape as
# core/founder_approval_detector.py — deliberately not imported from there
# since its `_upsert` is private/module-internal, matching the established,
# documented convention of small local re-implementations across routers).
# ---------------------------------------------------------------------------


def _existing_approval(dedupe_key: str) -> dict | None:
    rows = supabase.table(APPROVAL_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    return rows[0] if rows else None


def _create_or_refresh_approval(*, dedupe_key: str, related_entity_id: str, related_entity_type: str, **fields) -> str | None:
    existing = _existing_approval(dedupe_key)
    if existing is not None:
        if existing.get("status") in ("freigegeben", "abgelehnt", "archiviert"):
            return existing["id"]
        try:
            supabase.table(APPROVAL_TABLE).update({**fields, "updated_at": _now_iso()}).eq("dedupe_key", dedupe_key).execute()
        except Exception:
            pass
        return existing["id"]

    payload = {
        **fields,
        "dedupe_key": dedupe_key,
        "status": "ki_geprueft",
        "auto_detected": True,
        "related_entity_type": related_entity_type,
        "related_entity_id": related_entity_id,
        "priority": fields.get("priority", "mittel"),
    }
    try:
        response = supabase.table(APPROVAL_TABLE).insert(payload).execute()
    except Exception:
        return None
    return response.data[0]["id"] if response.data else None


# ---------------------------------------------------------------------------
# Task Manager integration (same shape as core/founder_task_detector.py)
# ---------------------------------------------------------------------------


def _create_or_refresh_task(*, dedupe_key: str, **fields) -> str | None:
    existing_rows = supabase.table(TASK_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing_rows:
        existing = existing_rows[0]
        if existing.get("status") in ("erledigt", "archiviert"):
            return existing["id"]
        try:
            supabase.table(TASK_TABLE).update({**fields, "updated_at": _now_iso()}).eq("dedupe_key", dedupe_key).execute()
        except Exception:
            pass
        return existing["id"]

    payload = {**fields, "dedupe_key": dedupe_key, "auto_detected": True}
    try:
        response = supabase.table(TASK_TABLE).insert(payload).execute()
    except Exception:
        return None
    return response.data[0]["id"] if response.data else None


# ---------------------------------------------------------------------------
# Alerts (deduplicated, prioritized)
# ---------------------------------------------------------------------------


def create_or_refresh_alert(*, dedupe_key: str, severity: str, title: str, message: str, category: str | None = None, source_run_id: str | None = None) -> str | None:
    existing_rows = supabase.table(ALERT_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing_rows:
        existing = existing_rows[0]
        if existing.get("status") == "archiviert":
            return existing["id"]
        try:
            supabase.table(ALERT_TABLE).update(
                {"severity": severity, "message": message, "updated_at": _now_iso()}
            ).eq("dedupe_key", dedupe_key).execute()
        except Exception:
            pass
        return existing["id"]
    payload = {
        "dedupe_key": dedupe_key, "severity": severity, "title": title, "message": message,
        "category": category, "source_run_id": source_run_id, "status": "offen",
    }
    try:
        response = supabase.table(ALERT_TABLE).insert(payload).execute()
    except Exception:
        return None
    return response.data[0]["id"] if response.data else None


# ---------------------------------------------------------------------------
# Action execution (dispatches into real, already-reviewed modules)
# ---------------------------------------------------------------------------


def _execute_action(action: dict, rule: dict) -> dict:
    """Executes exactly one Safe-Action-Registry action. Returns
    `{"status": "erfolgreich"|"fehlgeschlagen", "detail": ..., "previous_state": ...|None}`.
    Never raises — a failed action is reported, not propagated, so a
    multi-action rule can report partial success."""
    action_type = action.get("action_type")
    params = action.get("params", {})
    allowed, reason = registry.is_action_allowed(action_type, environment=rule.get("environment", "production"))
    if not allowed:
        return {"status": "fehlgeschlagen", "detail": reason, "previous_state": None}

    try:
        if action_type == "task_erstellen":
            task_id = _create_or_refresh_task(
                dedupe_key=params.get("dedupe_key") or f"automation_rule_{rule['id']}_task",
                title=params.get("title", rule.get("name", "Automatisierte Aufgabe")),
                category=params.get("category", "technik"),
                source="automation_engine",
                priority=params.get("priority", "mittel"),
                status="neu",
                reason=params.get("reason", f"Automatisch erstellt durch Regel '{rule.get('name')}'."),
                data_used=params.get("data_used", f"vt_automation_rules.id={rule['id']}"),
                impact_if_ignored=params.get("impact_if_ignored", "Wiederkehrendes operatives Problem bleibt ungelöst."),
                suggested_action_available=False,
            )
            return {"status": "erfolgreich" if task_id else "fehlgeschlagen", "detail": {"task_id": task_id}, "previous_state": None}

        if action_type == "approval_anfordern":
            approval_id = _create_or_refresh_approval(
                dedupe_key=params.get("dedupe_key") or f"automation_rule_{rule['id']}_approval",
                related_entity_id=params.get("related_entity_id", str(rule["id"])),
                related_entity_type=params.get("related_entity_type", "automation_rule"),
                title=params.get("title", rule.get("name", "Automatisierte Freigabeanfrage")),
                category=params.get("category", "automation"),
                source="automation_engine",
                reason=params.get("reason", f"Automatisch angefordert durch Regel '{rule.get('name')}'."),
                data_used=params.get("data_used", f"vt_automation_rules.id={rule['id']}"),
                rules_applied=params.get("rules_applied", "Regel-Aktion 'approval_anfordern'."),
                benefits=params.get("benefits", ""),
                risks=params.get("risks", ""),
            )
            return {"status": "erfolgreich" if approval_id else "fehlgeschlagen", "detail": {"approval_id": approval_id}, "previous_state": None}

        if action_type == "link_pruefen":
            product_id = params.get("product_id")
            product_rows = supabase.table(AFFILIATE_PRODUCT_TABLE).select("id,affiliate_url").eq("id", product_id).limit(1).execute().data or []
            if not product_rows:
                return {"status": "fehlgeschlagen", "detail": "Produkt nicht gefunden.", "previous_state": None}
            result = check_link(product_rows[0]["affiliate_url"])
            supabase.table(AFFILIATE_PRODUCT_TABLE).update(
                {"link_status": result["link_status"], "link_http_status": result["http_status"], "link_last_checked_at": _now_iso()}
            ).eq("id", product_id).execute()
            return {"status": "erfolgreich", "detail": result, "previous_state": None}

        if action_type == "affiliate_produkt_pausieren":
            product_id = params.get("product_id")
            product_rows = supabase.table(AFFILIATE_PRODUCT_TABLE).select("id,status").eq("id", product_id).limit(1).execute().data or []
            if not product_rows:
                return {"status": "fehlgeschlagen", "detail": "Produkt nicht gefunden.", "previous_state": None}
            previous_status = product_rows[0].get("status")
            supabase.table(AFFILIATE_PRODUCT_TABLE).update({"status": "paused"}).eq("id", product_id).execute()
            return {
                "status": "erfolgreich", "detail": {"product_id": product_id, "previous_status": previous_status, "new_status": "paused"},
                "previous_state": {"table": AFFILIATE_PRODUCT_TABLE, "id": product_id, "field": "status", "value": previous_status},
            }

        if action_type == "api_verbindung_testen":
            statuses = [s.__dict__ for s in get_provider_statuses()]
            return {"status": "erfolgreich", "detail": {"providers": statuses}, "previous_state": None}

        if action_type == "health_check_ausfuehren":
            report = get_full_integration_report()
            return {"status": "erfolgreich", "detail": {"integration_report": report}, "previous_state": None}

        if action_type == "analytics_snapshot_erstellen":
            snapshot = get_business_dashboard()
            return {"status": "erfolgreich", "detail": {"dashboard_snapshot": snapshot}, "previous_state": None}

        if action_type == "founder_alert_erzeugen":
            alert_id = create_or_refresh_alert(
                dedupe_key=params.get("dedupe_key") or f"automation_rule_{rule['id']}_alert",
                severity=params.get("severity", "mittel"),
                title=params.get("title", rule.get("name", "Automatisierungs-Warnung")),
                message=params.get("message", ""),
                category=rule.get("category"),
            )
            return {"status": "erfolgreich" if alert_id else "fehlgeschlagen", "detail": {"alert_id": alert_id}, "previous_state": None}

        if action_type == "regel_pausieren":
            target_rule_id = params.get("target_rule_id", rule["id"])
            previous_rows = supabase.table(RULE_TABLE).select("status,enabled").eq("id", target_rule_id).limit(1).execute().data or []
            previous = previous_rows[0] if previous_rows else {"status": rule.get("status"), "enabled": rule.get("enabled")}
            supabase.table(RULE_TABLE).update({"status": "pausiert", "enabled": False}).eq("id", target_rule_id).execute()
            return {
                "status": "erfolgreich", "detail": {"rule_id": target_rule_id, "new_status": "pausiert"},
                "previous_state": {"table": RULE_TABLE, "id": target_rule_id, "field": "status", "value": previous.get("status")},
            }

        if action_type == "workflow_stoppen":
            target_run_id = params.get("target_run_id")
            if target_run_id:
                supabase.table(RUN_TABLE).update({"status": "abgebrochen", "finished_at": _now_iso()}).eq("id", target_run_id).execute()
            return {"status": "erfolgreich", "detail": {"run_id": target_run_id, "new_status": "abgebrochen"}, "previous_state": None}

        return {"status": "fehlgeschlagen", "detail": f"Keine Ausführungslogik für '{action_type}'.", "previous_state": None}
    except Exception as exc:  # noqa: BLE001 — a failed action must never crash the run loop
        return {"status": "fehlgeschlagen", "detail": str(exc), "previous_state": None}


# ---------------------------------------------------------------------------
# Dry run (never mutates anything)
# ---------------------------------------------------------------------------


def dry_run_rule(rule_id: str) -> dict:
    rule = get_rule(rule_id)
    if rule is None:
        raise LookupError("Regel nicht gefunden.")

    context = _build_context(rule)
    trigger_due = _is_trigger_due(rule, context)
    conditions_met = evaluate_conditions(rule.get("conditions"), context)
    would_run = trigger_due and conditions_met
    requires_approval = rule.get("risk_level") in APPROVAL_REQUIRED_RISKS or rule.get("approval_policy") != "no_approval"

    trigger_signature = _trigger_signature(rule, context)
    idempotency_key = build_idempotency_key(str(rule["id"]), trigger_signature)
    duplicate_rows = supabase.table(RUN_TABLE).select("id,status").eq("idempotency_key", idempotency_key).limit(1).execute().data or []

    action_previews = []
    for action in rule.get("actions", []):
        allowed, reason = registry.is_action_allowed(action.get("action_type"), environment=rule.get("environment", "production"))
        action_previews.append({"action_type": action.get("action_type"), "would_execute": would_run and allowed, "blocked_reason": reason})

    record_audit_event(user_id=None, email=None, action="create", entity_type="automation_dry_run", entity_id=str(rule_id))

    return {
        "rule_id": str(rule_id),
        "trigger_recognized": trigger_due,
        "conditions_met": conditions_met,
        "context_used": context,
        "would_run": would_run,
        "actions_preview": action_previews,
        "requires_approval": requires_approval,
        "risk_level": rule.get("risk_level"),
        "possible_duplicate": bool(duplicate_rows),
        "note": "Dry Run — es wurden keine echten Änderungen vorgenommen.",
    }


# ---------------------------------------------------------------------------
# Context building per trigger type / category (only real, queryable data)
# ---------------------------------------------------------------------------


def _build_context(rule: dict) -> dict:
    context: dict = {"now_hour": _now().hour, "environment": rule.get("environment")}
    trigger_config = rule.get("trigger_config") or {}

    if rule.get("category") == "affiliate" or rule.get("trigger_type") in ("event", "threshold"):
        try:
            broken = supabase.table(AFFILIATE_PRODUCT_TABLE).select("id", count="exact").eq("link_status", "broken").execute()
            context["broken_links_count"] = broken.count or 0
        except Exception:
            context["broken_links_count"] = 0

    if rule.get("trigger_type") == "task_overdue":
        threshold_days = trigger_config.get("age_in_days", 3)
        try:
            open_tasks = supabase.table(TASK_TABLE).select("created_at").in_("status", ["neu", "in_bearbeitung", "warten"]).execute().data or []
        except Exception:
            open_tasks = []
        overdue = 0
        for task in open_tasks:
            created = task.get("created_at")
            if not created:
                continue
            try:
                parsed = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (_now() - parsed).days >= threshold_days:
                overdue += 1
        context["overdue_tasks_count"] = overdue

    if rule.get("trigger_type") == "approval_granted":
        related_entity_id = trigger_config.get("related_entity_id")
        if related_entity_id:
            rows = supabase.table(APPROVAL_TABLE).select("status").eq("related_entity_id", related_entity_id).limit(1).execute().data or []
            context["approval_status"] = rows[0]["status"] if rows else None

    return context


def _is_trigger_due(rule: dict, context: dict) -> bool:
    trigger_type = rule.get("trigger_type")
    if trigger_type == "manual":
        return False  # manual triggers never fire from the scheduler loop
    if trigger_type == "schedule":
        next_run_at = rule.get("next_run_at")
        if not next_run_at:
            return False
        try:
            parsed = datetime.fromisoformat(str(next_run_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        return _now() >= parsed
    # event / approval_granted / task_overdue / threshold: due exactly when
    # the rule's own conditions are met against the freshly built context —
    # there is no push-based event bus in this codebase, so "the event
    # happened" is detected by querying current state at evaluation time.
    return evaluate_conditions(rule.get("conditions"), context)


def _trigger_signature(rule: dict, context: dict) -> str:
    trigger_type = rule.get("trigger_type")
    if trigger_type == "schedule":
        return date.today().isoformat()
    return _canonical_signature({"trigger_type": trigger_type, "context": context})


# ---------------------------------------------------------------------------
# Execution + retry + dead letter
# ---------------------------------------------------------------------------


def _retry_policy(rule: dict) -> dict:
    policy = rule.get("retry_policy") or {}
    return {
        "type": policy.get("type", "none"),
        "max_attempts": int(policy.get("max_attempts", 1)),
        "cooldown_seconds": int(policy.get("cooldown_seconds", 60)),
    }


def _send_to_dead_letter(run: dict, rule: dict, *, reason: str) -> None:
    try:
        supabase.table(DEAD_LETTER_TABLE).insert(
            {"run_id": run["id"], "rule_id": rule["id"], "reason": reason, "payload": {"steps": run.get("steps", [])}}
        ).execute()
    except Exception:
        pass
    supabase.table(RUN_TABLE).update({"status": "dead_letter", "finished_at": _now_iso()}).eq("id", run["id"]).execute()
    _create_or_refresh_task(
        dedupe_key=f"automation_dead_letter_{run['id']}",
        title=f"Automatisierung fehlgeschlagen: {rule.get('name')}",
        category="technik",
        source="automation_engine",
        priority="hoch",
        status="neu",
        reason=f"Regel '{rule.get('name')}' hat die maximale Anzahl an Wiederholungsversuchen erreicht.",
        data_used=f"vt_automation_runs.id={run['id']}",
        impact_if_ignored="Der zugrunde liegende operative Ablauf bleibt unbearbeitet.",
        suggested_action_available=False,
    )
    create_or_refresh_alert(
        dedupe_key=f"automation_dead_letter_alert_{run['id']}",
        severity="hoch",
        title=f"Workflow gestoppt: {rule.get('name')}",
        message=reason,
        category=rule.get("category"),
        source_run_id=run["id"],
    )
    record_audit_event(user_id=None, email=None, action="update", entity_type="automation_run", entity_id=str(run["id"]), metadata={"event": "dead_letter"})


def execute_rule_run(rule: dict, *, existing_run: dict | None = None, dry_run: bool = False) -> dict:
    """Executes (or retries) one run of `rule`. Never raises."""
    trigger_signature = _trigger_signature(rule, _build_context(rule))
    idempotency_key = build_idempotency_key(str(rule["id"]), trigger_signature)

    if existing_run is None:
        payload = {
            "rule_id": rule["id"], "idempotency_key": idempotency_key, "trigger_type": rule.get("trigger_type"),
            "trigger_signature": trigger_signature, "status": "laeuft", "risk_level": rule.get("risk_level"),
            "environment": rule.get("environment"), "attempt": 1, "max_attempts": _retry_policy(rule)["max_attempts"],
            "dry_run": dry_run, "steps": [], "started_at": _now_iso(),
        }
        response = supabase.table(RUN_TABLE).insert(payload).execute()
        run = response.data[0] if response.data else payload
    else:
        run = existing_run
        supabase.table(RUN_TABLE).update({"status": "laeuft", "attempt": run["attempt"] + 1}).eq("id", run["id"]).execute()
        run["attempt"] += 1

    steps: list[dict] = []
    all_succeeded = True
    any_succeeded = False
    previous_states: list[dict] = []

    for action in rule.get("actions", []):
        result = _execute_action(action, rule)
        steps.append({"action_type": action.get("action_type"), "status": result["status"], "detail": result["detail"], "at": _now_iso()})
        if result["status"] == "erfolgreich":
            any_succeeded = True
            if result.get("previous_state"):
                previous_states.append(result["previous_state"])
        else:
            all_succeeded = False

    final_status = "erfolgreich" if all_succeeded else ("teilweise_erfolgreich" if any_succeeded else "fehlgeschlagen")
    retry_policy = _retry_policy(rule)

    if final_status == "fehlgeschlagen" and retry_policy["type"] != "none" and run["attempt"] < retry_policy["max_attempts"]:
        final_status = "fehlgeschlagen_wird_wiederholt"

    update_payload = {
        "status": final_status, "steps": steps, "result": {"summary": final_status},
        "previous_state": previous_states or None, "finished_at": _now_iso() if final_status in TERMINAL_RUN_STATUSES or final_status == "teilweise_erfolgreich" else None,
        "updated_at": _now_iso(),
    }
    supabase.table(RUN_TABLE).update(update_payload).eq("id", run["id"]).execute()
    run.update(update_payload)

    if final_status == "fehlgeschlagen_wird_wiederholt":
        pass  # left open for the next evaluate_and_run_due_rules() pass
    elif final_status not in ("fehlgeschlagen_wird_wiederholt",) and run["attempt"] >= retry_policy["max_attempts"] and final_status == "fehlgeschlagen":
        _send_to_dead_letter(run, rule, reason="Maximale Wiederholungsversuche erreicht.")

    if final_status in ("erfolgreich", "teilweise_erfolgreich"):
        rule_update = {
            "last_run_at": _now_iso(), "run_count": rule.get("run_count", 0) + 1, "updated_at": _now_iso(),
        }
        if rule.get("trigger_type") == "schedule":
            rule_update["next_run_at"] = _rule_default_next_run_at(rule.get("trigger_config", {}))
        supabase.table(RULE_TABLE).update(rule_update).eq("id", rule["id"]).execute()

    record_audit_event(
        user_id=None, email=None, action="update", entity_type="automation_run", entity_id=str(run["id"]),
        metadata={"event": "workflow_beendet", "status": final_status},
    )
    return run


def evaluate_and_run_due_rules(*, environment: str | None = None) -> dict:
    """The one non-manual entry point that decides + executes rules. Called
    on every dashboard load and by `POST /automation/run-due`."""
    query = supabase.table(RULE_TABLE).select("*").eq("enabled", True).eq("status", "aktiv")
    if environment:
        query = query.eq("environment", environment)
    rules = query.execute().data or []

    executed, skipped, awaiting_approval = [], [], []

    for rule in rules:
        context = _build_context(rule)
        if not _is_trigger_due(rule, context):
            continue
        if not evaluate_conditions(rule.get("conditions"), context):
            continue

        trigger_signature = _trigger_signature(rule, context)
        idempotency_key = build_idempotency_key(str(rule["id"]), trigger_signature)
        existing_rows = supabase.table(RUN_TABLE).select("*").eq("idempotency_key", idempotency_key).limit(1).execute().data or []
        existing_run = existing_rows[0] if existing_rows else None

        if existing_run and existing_run.get("status") in TERMINAL_RUN_STATUSES:
            skipped.append(rule["id"])
            continue

        if existing_run and existing_run.get("status") == "fehlgeschlagen_wird_wiederholt":
            retry_policy = _retry_policy(rule)
            updated_at = existing_run.get("updated_at")
            if updated_at:
                try:
                    parsed = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                    if (_now() - parsed).total_seconds() < retry_policy["cooldown_seconds"]:
                        skipped.append(rule["id"])
                        continue
                except ValueError:
                    pass
            run = execute_rule_run(rule, existing_run=existing_run)
            executed.append(run["id"])
            continue

        risk_level = rule.get("risk_level")
        requires_approval = risk_level in APPROVAL_REQUIRED_RISKS or rule.get("approval_policy") == "always_require_approval"
        one_time_ok = rule.get("approval_policy") == "one_time_approval" and rule.get("approved_once")

        if requires_approval and not one_time_ok and existing_run is None:
            run_payload = {
                "rule_id": rule["id"], "idempotency_key": idempotency_key, "trigger_type": rule.get("trigger_type"),
                "trigger_signature": trigger_signature, "status": "wartet_auf_freigabe", "risk_level": risk_level,
                "environment": rule.get("environment"), "attempt": 0, "max_attempts": _retry_policy(rule)["max_attempts"],
                "steps": [],
            }
            response = supabase.table(RUN_TABLE).insert(run_payload).execute()
            run = response.data[0] if response.data else run_payload
            approval_id = _create_or_refresh_approval(
                dedupe_key=f"automation_run_{run.get('id')}",
                related_entity_id=str(run.get("id")),
                related_entity_type="automation_run",
                title=f"Automatisierungslauf freigeben: {rule.get('name')}",
                category="automation",
                source="automation_engine",
                reason=f"Regel '{rule.get('name')}' (Risk Level: {risk_level}) ist zur Ausführung bereit und benötigt eine Freigabe.",
                data_used=f"vt_automation_rules.id={rule['id']}, run.id={run.get('id')}",
                rules_applied="approval_policy erfordert Freigabe pro Lauf.",
                benefits="", risks=f"Risk Level: {risk_level}.",
            )
            if approval_id:
                supabase.table(RUN_TABLE).update({"approval_id": approval_id}).eq("id", run.get("id")).execute()
            awaiting_approval.append(run.get("id"))
            continue

        run = execute_rule_run(rule, existing_run=None)
        executed.append(run["id"])

    return {"executed": executed, "skipped": skipped, "awaiting_approval": awaiting_approval, "evaluated_at": _now_iso()}


def run_rule_manually(rule_id: str, *, requested_by: str) -> dict:
    rule = get_rule(rule_id)
    if rule is None:
        raise LookupError("Regel nicht gefunden.")
    if rule.get("status") == "archiviert":
        raise ValueError("Archivierte Regeln können nicht ausgeführt werden.")
    record_audit_event(user_id=None, email=requested_by, action="update", entity_type="automation_rule", entity_id=rule_id, metadata={"event": "manueller_lauf_ausgeloest"})
    return execute_rule_run(rule, existing_run=None)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def rollback_run(run_id: str, *, requested_by: str) -> dict:
    rows = supabase.table(RUN_TABLE).select("*").eq("id", run_id).limit(1).execute().data or []
    if not rows:
        raise LookupError("Run nicht gefunden.")
    run = rows[0]
    if run.get("status") != "erfolgreich":
        raise ValueError("Nur erfolgreiche Läufe können zurückgerollt werden.")
    if run.get("rollback_status") == "durchgefuehrt":
        raise ValueError("Dieser Lauf wurde bereits zurückgerollt.")
    previous_states = run.get("previous_state")
    if not previous_states:
        raise ValueError("Für diesen Lauf ist kein Rollback verfügbar (Aktion nicht reversibel).")

    for state in previous_states:
        try:
            supabase.table(state["table"]).update({state["field"]: state["value"]}).eq("id", state["id"]).execute()
        except Exception:
            pass

    supabase.table(RUN_TABLE).update(
        {"rollback_status": "durchgefuehrt", "rollback_at": _now_iso(), "rollback_by": requested_by, "status": "zurueckgerollt"}
    ).eq("id", run_id).execute()
    record_audit_event(user_id=None, email=requested_by, action="update", entity_type="automation_run", entity_id=str(run_id), metadata={"event": "rollback"})
    return {"run_id": run_id, "status": "zurueckgerollt"}


# ---------------------------------------------------------------------------
# Daily Briefing integration (read-only summary function; called from
# `routers/founder_briefing.py` — a small, additive extension, not a
# parallel briefing system)
# ---------------------------------------------------------------------------


def get_daily_briefing_summary() -> dict:
    try:
        today_start = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc).isoformat()
        runs_today = supabase.table(RUN_TABLE).select("status").gte("created_at", today_start).execute().data or []
    except Exception:
        runs_today = []
    auto_completed = sum(1 for r in runs_today if r.get("status") == "erfolgreich")
    failed = sum(1 for r in runs_today if r.get("status") in ("fehlgeschlagen", "dead_letter"))
    awaiting_approval = sum(1 for r in runs_today if r.get("status") == "wartet_auf_freigabe")
    try:
        alerts = supabase.table(ALERT_TABLE).select("severity").eq("status", "offen").execute().data or []
    except Exception:
        alerts = []
    important_warnings = sum(1 for a in alerts if a.get("severity") in ("hoch", "kritisch"))
    return {
        "auto_completed_today": auto_completed,
        "failed_today": failed,
        "awaiting_approval": awaiting_approval,
        "important_warnings": important_warnings,
    }
