"""Founder Autopilot — Smart Alerts (VitalTwin Enterprise, Founder
Operating System, Submodule J).

Real signal only — reuses the same dedupe/priority pattern as Submodule
G's alerts (`core/automation_engine.py::create_or_refresh_alert`), in its
own `vt_founder_autopilot_alerts` table since these are cross-module
"founder attention" alerts, not single-rule automation alerts.

Several examples named in the spec have no real data source in this
codebase (Stripe payment-failure rate, AI cost spikes, backup overdue —
Supabase manages backups itself, production build failures — no CI/CD
integration) and are therefore honestly NOT implemented as detectors
here — implementing them would mean fabricating a signal.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import affiliate_provider
from .supabase import supabase

ALERT_TABLE = "vt_founder_autopilot_alerts"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_alert(*, dedupe_key: str, severity: str, title: str, message: str, category: str | None) -> None:
    existing = supabase.table(ALERT_TABLE).select("id,status").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing:
        if existing[0].get("status") == "archiviert":
            return
        supabase.table(ALERT_TABLE).update({"severity": severity, "message": message, "updated_at": _now_iso()}).eq("dedupe_key", dedupe_key).execute()
        return
    try:
        supabase.table(ALERT_TABLE).insert({
            "dedupe_key": dedupe_key, "severity": severity, "title": title, "message": message,
            "category": category, "status": "offen", "escalated": False,
        }).execute()
    except Exception:
        pass


def _detect_repeated_automation_failures() -> None:
    try:
        dead_letters = supabase.table("vt_automation_dead_letters").select("id,rule_id,created_at").execute().data or []
    except Exception:
        dead_letters = []
    if len(dead_letters) >= 3:
        _upsert_alert(
            dedupe_key="autopilot_repeated_automation_failures",
            severity="hoch", title="Mehrere Automationen sind endgültig fehlgeschlagen",
            message=f"{len(dead_letters)} Automatisierungsläufe haben die maximale Anzahl an Wiederholungsversuchen erreicht.",
            category="automatisierung",
        )


def _detect_broken_links_in_important_category() -> None:
    try:
        products = supabase.table("vt_affiliate_products").select("link_status,pinned").execute().data or []
    except Exception:
        products = []
    important_broken = [p for p in products if p.get("link_status") == "broken" and p.get("pinned")]
    if len(important_broken) >= 1:
        _upsert_alert(
            dedupe_key="autopilot_broken_links_important",
            severity="mittel", title="Defekte Links bei angepinnten Produkten",
            message=f"{len(important_broken)} angepinnte Affiliate-Produkte haben einen defekten Link.",
            category="affiliate",
        )


def _detect_affiliate_provider_down() -> None:
    statuses = affiliate_provider.get_provider_statuses()
    failing = [s for s in statuses if s.kind == "network_api" and s.connection_tested and not s.configured]
    if failing:
        _upsert_alert(
            dedupe_key="autopilot_affiliate_provider_down",
            severity="mittel", title="Affiliate-Provider nicht erreichbar",
            message=f"{len(failing)} Affiliate-Provider melden ein Verbindungsproblem.",
            category="affiliate",
        )


def run_alert_detection() -> None:
    _detect_repeated_automation_failures()
    _detect_broken_links_in_important_category()
    _detect_affiliate_provider_down()


def list_alerts(*, status: str | None = None) -> list[dict]:
    items = supabase.table(ALERT_TABLE).select("*").order("created_at", desc=True).execute().data or []
    if status:
        items = [i for i in items if i.get("status") == status]
    return items


def close_alert(alert_id: str) -> None:
    supabase.table(ALERT_TABLE).update({"status": "archiviert", "updated_at": _now_iso()}).eq("id", alert_id).execute()


def escalate_alert(alert_id: str) -> None:
    supabase.table(ALERT_TABLE).update({"escalated": True, "severity": "kritisch", "updated_at": _now_iso()}).eq("id", alert_id).execute()
