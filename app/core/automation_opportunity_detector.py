"""Automation Engine — Automation Opportunity Detection (VitalTwin
Enterprise, Founder Operating System, Submodule G).

Detects **recurring manual patterns** the founder is already doing by
hand — via the Task Manager and Smart Approval Center tables that already
exist — and writes a *suggestion only*. No rule is ever created or
activated automatically; a human (the founder) must review the
suggestion and, if useful, use it to pre-fill a draft rule via
`core/automation_engine.py::create_rule`.

**Real signal, no invented pattern-matching.** Two concrete, queryable
recurring patterns are detected:

1. Approval requests with the same `source` repeatedly approved
   (`vt_founder_approvals`, `status='freigegeben'`) — a strong signal the
   founder keeps making the same yes/no decision.
2. Tasks in the same `category` + `source` repeatedly resolved
   (`vt_founder_tasks`, `status='erledigt'`, `auto_resolved=False` —
   i.e. the founder closed it themselves, not the system).

Both are counted over a rolling 30-day window; five or more occurrences
triggers a suggestion (idempotent via `signature`).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from .supabase import supabase

OPPORTUNITY_TABLE = "vt_automation_opportunities"
APPROVAL_TABLE = "vt_founder_approvals"
TASK_TABLE = "vt_founder_tasks"

OCCURRENCE_THRESHOLD = 5
LOOKBACK_DAYS = 30


def _window_start_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()


def _upsert_opportunity(*, signature: str, category: str | None, description: str, occurrences: int, source_table: str, suggested_rule: dict) -> None:
    existing_rows = supabase.table(OPPORTUNITY_TABLE).select("*").eq("signature", signature).limit(1).execute().data or []
    if existing_rows:
        existing = existing_rows[0]
        if existing.get("status") in ("abgelehnt", "regel_erstellt"):
            return  # founder already decided — never reopen or re-suggest
        try:
            supabase.table(OPPORTUNITY_TABLE).update(
                {"occurrences": occurrences, "description": description, "updated_at": datetime.now(timezone.utc).isoformat()}
            ).eq("signature", signature).execute()
        except Exception:
            pass
        return
    try:
        supabase.table(OPPORTUNITY_TABLE).insert(
            {
                "signature": signature, "category": category, "description": description,
                "occurrences": occurrences, "source_table": source_table, "suggested_rule": suggested_rule,
                "status": "neu",
            }
        ).execute()
    except Exception:
        pass


def _detect_repeated_approvals() -> None:
    try:
        rows = (
            supabase.table(APPROVAL_TABLE).select("source,category,decided_at")
            .eq("status", "freigegeben").gte("decided_at", _window_start_iso()).execute().data or []
        )
    except Exception:
        rows = []
    counts = Counter((r.get("source"), r.get("category")) for r in rows if r.get("source"))
    for (source, category), count in counts.items():
        if count < OCCURRENCE_THRESHOLD:
            continue
        _upsert_opportunity(
            signature=f"approval:{source}",
            category=category,
            description=(
                f"Dieser Ablauf wurde {count}-mal ähnlich durchgeführt: Freigabe-Anfragen aus "
                f"'{source}' wurden in den letzten {LOOKBACK_DAYS} Tagen wiederholt freigegeben. "
                "Soll eine Automatisierungsregel vorbereitet werden?"
            ),
            occurrences=count,
            source_table=APPROVAL_TABLE,
            suggested_rule={
                "name": f"Automatische Freigabe-Vorbereitung: {source}",
                "category": category or "affiliate",
                "trigger_type": "event",
                "risk_level": "low",
                "approval_policy": "always_require_approval",
                "actions": [{"action_type": "approval_anfordern", "params": {}}],
            },
        )


def _detect_repeated_task_resolutions() -> None:
    try:
        rows = (
            supabase.table(TASK_TABLE).select("category,source,status,auto_resolved,updated_at")
            .eq("status", "erledigt").eq("auto_resolved", False).gte("updated_at", _window_start_iso()).execute().data or []
        )
    except Exception:
        rows = []
    counts = Counter((r.get("category"), r.get("source")) for r in rows)
    for (category, source), count in counts.items():
        if count < OCCURRENCE_THRESHOLD:
            continue
        _upsert_opportunity(
            signature=f"task:{category}:{source}",
            category=category,
            description=(
                f"Dieser Ablauf wurde {count}-mal ähnlich durchgeführt: Aufgaben aus '{source}' "
                f"({category}) wurden in den letzten {LOOKBACK_DAYS} Tagen wiederholt manuell "
                "erledigt. Soll eine Automatisierungsregel vorbereitet werden?"
            ),
            occurrences=count,
            source_table=TASK_TABLE,
            suggested_rule={
                "name": f"Automatische Aufgabenbearbeitung: {source}",
                "category": category or "founder_tasks",
                "trigger_type": "event",
                "risk_level": "low",
                "approval_policy": "no_approval",
                "actions": [{"action_type": "task_erstellen", "params": {}}],
            },
        )


def run_opportunity_detection() -> None:
    _detect_repeated_approvals()
    _detect_repeated_task_resolutions()


def dismiss_opportunity(opportunity_id: str) -> None:
    supabase.table(OPPORTUNITY_TABLE).update(
        {"status": "abgelehnt", "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", opportunity_id).execute()


def mark_opportunity_rule_created(opportunity_id: str) -> None:
    supabase.table(OPPORTUNITY_TABLE).update(
        {"status": "regel_erstellt", "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", opportunity_id).execute()
