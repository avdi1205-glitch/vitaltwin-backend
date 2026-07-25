"""CEO Intelligence — Executive Risk & Opportunity Center (VitalTwin
Enterprise, Founder Operating System, Submodule H).

**Aggregation-only, no parallel storage.** Risks and opportunities are
never re-persisted into a new table — they are read-time views built from
already-existing rows:

- `vt_founder_business_insights` (Submodule E) — category contains
  "risiko"/"chance".
- `vt_automation_alerts` (Submodule G) — open alerts become risks.
- `vt_automation_opportunities` (Submodule G) — open suggestions become
  opportunities.
- `core/affiliate_product_health.py` (Submodule F) — products with
  `status == "critical"` become risks (read-only, no re-computation of
  health rules here).

Closing/archiving an executive risk or opportunity always updates the
**underlying** row's own status field — never a second, disconnected
status.

**One genuinely new detection rule**: `detect_data_quality_risk()` — the
one insight category the spec asks for that has real, computable signal
in this codebase (how many CEO Overview metrics are degraded). Writes
into the SAME `vt_founder_business_insights` table (category
`datenqualitaetsrisiko`), reusing the exact idempotent
`dedupe_key`-guarded upsert pattern already used by every other Founder-
OS detector.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from . import affiliate_product_health as product_health
from . import executive_metrics as ex_metrics
from .supabase import supabase

INSIGHT_TABLE = "vt_founder_business_insights"
ALERT_TABLE = "vt_automation_alerts"
OPPORTUNITY_TABLE = "vt_automation_opportunities"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
TASK_TABLE = "vt_founder_tasks"
APPROVAL_TABLE = "vt_founder_approvals"

OPEN_INSIGHT_STATUSES = {"erkannt", "geprueft", "zur_freigabe_gesendet"}
TERMINAL_INSIGHT_STATUSES = {"umgesetzt", "verworfen", "archiviert"}

# Categories where the underlying business decision (price, campaign,
# partner, budget, ...) always needs a founder Approval-Center decision —
# never automatically actioned.
APPROVAL_REQUIRED_CATEGORIES = {"umsatzrisiko", "kostenrisiko", "premium_chance", "marktchance", "affiliate_chance"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_risk_from_insight(insight: dict) -> dict:
    return {
        "ref": f"insight:{insight['id']}", "title": insight.get("title"), "category": insight.get("category"),
        "severity": insight.get("severity", "mittel"), "probability": None,
        "possible_impact": insight.get("possible_impact"), "data_basis": insight.get("data_basis"),
        "affected_systems": ["business_coach"], "recommended_action": insight.get("recommended_action"),
        "responsible_module": "AI Business Coach", "status": insight.get("status"), "deadline": None,
        "approval_required": (insight.get("category") or "") in APPROVAL_REQUIRED_CATEGORIES,
        "source_table": INSIGHT_TABLE, "source_id": insight["id"],
    }


def _to_opportunity_from_insight(insight: dict) -> dict:
    return {
        "ref": f"insight:{insight['id']}", "title": insight.get("title"), "category": insight.get("category"),
        "expected_benefit": insight.get("expected_benefit"), "data_basis": insight.get("data_basis"),
        "effort": insight.get("estimated_effort"), "risk": None, "time_horizon": None,
        "decision_required": insight.get("recommended_action"), "next_step": insight.get("recommended_action"),
        "responsible_module": "AI Business Coach", "status": insight.get("status"),
        "source_table": INSIGHT_TABLE, "source_id": insight["id"],
    }


def _to_risk_from_alert(alert: dict) -> dict:
    severity_map = {"kritisch": "kritisch", "hoch": "hoch", "mittel": "mittel", "niedrig": "niedrig"}
    return {
        "ref": f"alert:{alert['id']}", "title": alert.get("title"), "category": "automatisierungsrisiko",
        "severity": severity_map.get(alert.get("severity"), "mittel"), "probability": None,
        "possible_impact": alert.get("message"), "data_basis": "vt_automation_alerts",
        "affected_systems": ["automation_engine"], "recommended_action": alert.get("message"),
        "responsible_module": "Automation Engine", "status": alert.get("status"), "deadline": None,
        "approval_required": False, "source_table": ALERT_TABLE, "source_id": alert["id"],
    }


def _to_opportunity_from_automation(opportunity: dict) -> dict:
    return {
        "ref": f"automation_opportunity:{opportunity['id']}", "title": opportunity.get("description"),
        "category": "automatisierungschance", "expected_benefit": "Wiederkehrende manuelle Arbeit reduzieren.",
        "data_basis": f"{opportunity.get('occurrences')} ähnliche Vorgänge (30 Tage).", "effort": "gering",
        "risk": None, "time_horizon": None, "decision_required": "Automatisierungsregel erstellen?",
        "next_step": "Regel-Entwurf im Automation-Engine-Tab prüfen.", "responsible_module": "Automation Engine",
        "status": opportunity.get("status"), "source_table": OPPORTUNITY_TABLE, "source_id": opportunity["id"],
    }


def _to_risk_from_product_health(item: dict) -> dict:
    return {
        "ref": f"affiliate_product:{item['product_id']}", "title": f"Kritischer Produktstatus: {item.get('title')}",
        "category": "affiliate_risiko", "severity": "hoch", "probability": None,
        "possible_impact": "; ".join(item.get("reasons", [])) or None, "data_basis": "core/affiliate_product_health.py",
        "affected_systems": ["affiliate_intelligence"], "recommended_action": "Produkt im Affiliate-Intelligence-Tab prüfen.",
        "responsible_module": "Affiliate Intelligence", "status": "offen", "deadline": None,
        "approval_required": False, "source_table": AFFILIATE_PRODUCT_TABLE, "source_id": item["product_id"],
    }


def list_executive_risks() -> list[dict]:
    risks: list[dict] = []
    try:
        insights = supabase.table(INSIGHT_TABLE).select("*").execute().data or []
    except Exception:
        insights = []
    risks += [_to_risk_from_insight(i) for i in insights if "risiko" in (i.get("category") or "") and i.get("status") in OPEN_INSIGHT_STATUSES]

    try:
        alerts = supabase.table(ALERT_TABLE).select("*").eq("status", "offen").execute().data or []
    except Exception:
        alerts = []
    risks += [_to_risk_from_alert(a) for a in alerts]

    try:
        products = supabase.table(AFFILIATE_PRODUCT_TABLE).select("*").execute().data or []
    except Exception:
        products = []
    for p in products:
        health = product_health.compute_product_health(p, blacklisted=False)
        if health["status"] == "critical":
            risks.append(_to_risk_from_product_health({"product_id": p["id"], "title": p.get("title"), "reasons": health["reasons"]}))

    return risks


def list_executive_opportunities() -> list[dict]:
    opportunities: list[dict] = []
    try:
        insights = supabase.table(INSIGHT_TABLE).select("*").execute().data or []
    except Exception:
        insights = []
    opportunities += [_to_opportunity_from_insight(i) for i in insights if "chance" in (i.get("category") or "") and i.get("status") in OPEN_INSIGHT_STATUSES]

    try:
        automation_opportunities = supabase.table(OPPORTUNITY_TABLE).select("*").eq("status", "neu").execute().data or []
    except Exception:
        automation_opportunities = []
    opportunities += [_to_opportunity_from_automation(o) for o in automation_opportunities]

    return opportunities


def _resolve_ref(ref: str) -> tuple[str, str]:
    table_key, _, source_id = ref.partition(":")
    table_map = {"insight": INSIGHT_TABLE, "alert": ALERT_TABLE, "automation_opportunity": OPPORTUNITY_TABLE, "affiliate_product": AFFILIATE_PRODUCT_TABLE}
    table = table_map.get(table_key)
    if not table or not source_id:
        raise ValueError("Unbekannte Referenz.")
    return table, source_id


def close_executive_risk(ref: str, *, closed_by: str) -> None:
    table, source_id = _resolve_ref(ref)
    if table == INSIGHT_TABLE:
        supabase.table(table).update({"status": "archiviert", "updated_at": _now_iso()}).eq("id", source_id).execute()
    elif table == ALERT_TABLE:
        supabase.table(table).update({"status": "archiviert", "updated_at": _now_iso()}).eq("id", source_id).execute()
    else:
        raise ValueError("Dieser Risiko-Typ kann hier nicht direkt geschlossen werden — bitte im zuständigen Modul bearbeiten.")


def archive_executive_opportunity(ref: str, *, archived_by: str) -> None:
    table, source_id = _resolve_ref(ref)
    if table == INSIGHT_TABLE:
        supabase.table(table).update({"status": "verworfen", "updated_at": _now_iso()}).eq("id", source_id).execute()
    elif table == OPPORTUNITY_TABLE:
        supabase.table(table).update({"status": "abgelehnt", "updated_at": _now_iso()}).eq("id", source_id).execute()
    else:
        raise ValueError("Diese Chance kann hier nicht direkt archiviert werden — bitte im zuständigen Modul bearbeiten.")


def _create_or_refresh_task(*, dedupe_key: str, **fields) -> str | None:
    existing_rows = supabase.table(TASK_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing_rows:
        return existing_rows[0]["id"]
    payload = {**fields, "dedupe_key": dedupe_key, "auto_detected": True}
    try:
        response = supabase.table(TASK_TABLE).insert(payload).execute()
    except Exception:
        return None
    return response.data[0]["id"] if response.data else None


def send_to_task_manager(ref: str, *, title: str, reason: str, category: str = "business") -> str | None:
    dedupe_key = f"ceo_intelligence_{ref}"
    return _create_or_refresh_task(
        dedupe_key=dedupe_key, title=title, category=category, source="ceo_intelligence", priority="hoch",
        status="neu", reason=reason, data_used=ref, impact_if_ignored="Strategisch relevanter Punkt bleibt unbearbeitet.",
        suggested_action=None, suggested_action_available=False,
    )


def _create_or_refresh_approval(*, dedupe_key: str, **fields) -> str | None:
    existing_rows = supabase.table(APPROVAL_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing_rows:
        return existing_rows[0]["id"]
    payload = {**fields, "dedupe_key": dedupe_key, "status": "ki_geprueft", "auto_detected": True}
    try:
        response = supabase.table(APPROVAL_TABLE).insert(payload).execute()
    except Exception:
        return None
    return response.data[0]["id"] if response.data else None


def send_to_approval_center(ref: str, *, title: str, reason: str, category: str = "business") -> str | None:
    """Never executes a decision — only creates a tracked proposal. There
    is no automatic side effect for strategic categories (Preis,
    Kampagne, Budget, ...): those remain manual, founder-only business
    decisions made outside this app, consistent with the "kein
    critical-Risiko" principle from Submodule G."""
    dedupe_key = f"ceo_intelligence_approval_{ref}"
    return _create_or_refresh_approval(
        dedupe_key=dedupe_key, title=title, category=category, source="ceo_intelligence", priority="hoch",
        reason=reason, data_used=ref, rules_applied="Manuell durch CEO Intelligence gesendet.", benefits="", risks="",
    )


# ---------------------------------------------------------------------------
# Data Quality Risk detector (the one genuinely new insight rule)
# ---------------------------------------------------------------------------

DEGRADED_QUALITY = {"nicht_verbunden", "unzureichend", "widersprüchlich", "veraltet"}
CRITICAL_METRIC_KEYS = ["revenue_today", "revenue_month", "ai_cost", "infra_cost", "product_status", "release_status"]


def detect_data_quality_risk() -> None:
    overview = ex_metrics.get_ceo_overview()
    degraded = [key for key in CRITICAL_METRIC_KEYS if overview.get(key, {}).get("data_quality") in DEGRADED_QUALITY]
    dedupe_key = f"ceo_data_quality_{date.today().isoformat()}"

    try:
        existing_rows = supabase.table(INSIGHT_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    except Exception:
        return
    existing = existing_rows[0] if existing_rows else None

    if len(degraded) < 4:
        return  # Not significant enough to raise as a strategic risk.
    if existing is not None and existing.get("status") in TERMINAL_INSIGHT_STATUSES:
        return

    payload = {
        "title": f"{len(degraded)} zentrale Kennzahlen sind nicht sauber verbunden",
        "category": "datenqualitaetsrisiko",
        "description": f"Folgende zentrale Kennzahlen haben keine ausreichende Datenqualität: {', '.join(degraded)}.",
        "data_basis": "core/executive_metrics.py::get_ceo_overview() — data_quality je Kennzahl.",
        "severity": "mittel", "confidence": "hoch",
        "possible_cause": "Fehlende Integrationen (Stripe-Reporting, OpenAI-Nutzungs-API, Release-Tracking).",
        "possible_impact": "Strategische Entscheidungen basieren auf unvollständigen Daten.",
        "recommended_action": "Fehlende Integrationen priorisieren (siehe jeweilige note-Felder).",
        "estimated_effort": "hoch", "expected_benefit": "Verlässlichere strategische Entscheidungsgrundlage.",
        "source_references": {"degraded_metrics": degraded},
    }

    if existing is not None:
        payload["updated_at"] = _now_iso()
        try:
            supabase.table(INSIGHT_TABLE).update(payload).eq("dedupe_key", dedupe_key).execute()
        except Exception:
            pass
        return

    payload.update({"dedupe_key": dedupe_key, "status": "erkannt", "source": "regelbasiert"})
    try:
        supabase.table(INSIGHT_TABLE).insert(payload).execute()
    except Exception:
        pass
