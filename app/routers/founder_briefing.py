"""Founder Daily Briefing — VitalTwin Release F2 (Founder Operating
System, Module 2).

Mounted at `/api/admin/founder` in `app/main.py` (same prefix as
`routers/founder.py`, kept as a separate file for module isolation). One
read-only endpoint, `GET /daily-briefing`, that is *generated fresh on
every request* from real data — there is no stored/cached "briefing"
record and nothing runs in the background. "Automatisch erstellt" here
means "computed automatically when the founder opens the page", not
"scheduled" — per spec, no cron/background job, no email, no push
notification is built.

**No LLM call happens in this module.** The "KI Empfehlungen" and "CEO
Prioritäten" sections are deterministic, auditable rule outputs over real
computed deltas (e.g. "5 neue Affiliate-Produkte gefunden" from an actual
`created_at >= today` count) — never a free-text generation, and never a
metric we cannot actually compute (e.g. no API response-time insight is
produced, because no response-time tracking exists anywhere in this
codebase — fabricating one would violate the "keine Fake-Daten" mandate).

**No placeholders, no demo data, no fabricated numbers.** Every numeric
field is either a real number computed right now, or an explicit `None`
with a `*_note` explaining what's missing — matching the same honesty
pattern as `routers/founder.py` and `routers/admin.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header

from ..core.admin_rbac import require_admin_permission
from ..core import automation_engine
from ..core import executive_summary
from ..core import documentation_score
from ..core.supabase import supabase

router = APIRouter()

USER_TABLE = "vt_users"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
CHAT_USAGE_TABLE = "vt_chat_usage"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
AFFILIATE_EVENT_TABLE = "vt_affiliate_events"
FEEDBACK_TABLE = "vt_user_feedback"

NO_STRIPE_REPORTING_NOTE = "Kein Stripe-Reporting implementiert (erfordert Stripe-Reporting-API-Anbindung)."
NO_PREMIUM_TIMESTAMP_NOTE = (
    "Keine zeitgestempelten Premium-Aktivierungen gespeichert (der Stripe-Webhook setzt nur das "
    "boolesche premium-Flag, ohne Aktivierungsdatum) — daher nicht rückwirkend zählbar."
)
NO_CANCELLATION_NOTE = "Keine Kündigungs-/Downgrade-Erfassung implementiert (Stripe-Webhook behandelt nur checkout.session.completed)."
NO_COST_NOTE = "Kein Kosten-Tracking implementiert (erfordert OpenAI-Nutzungs-API-Anbindung)."
NO_ERROR_TRACKING_NOTE = "Kein Error-Tracking-System integriert."
NO_LATENCY_NOTE = "Keine Antwortzeit-Messung implementiert."
NO_SERVER_MONITORING_NOTE = "Keine Server-Monitoring-Integration vorhanden."
NO_BUILD_STATUS_NOTE = "Keine CI/CD-Status-Integration vorhanden."
NO_BACKUP_MONITORING_NOTE = "Keine Backup-Monitoring-Integration vorhanden — Backups werden ausschließlich von Supabase selbst verwaltet."
NO_RELEASE_TRACKING_NOTE = "Keine Release-Tracking-Integration vorhanden."
NO_BUG_TRACKING_NOTE = "Kein Bug-Tracking-System integriert."
NO_DOC_FRESHNESS_NOTE = "Keine automatische Dokumentations-Aktualitätsprüfung implementiert."


def _count(table: str, *, filters: dict[str, object] | None = None, gte: tuple[str, str] | None = None) -> int | None:
    """Best-effort exact row count — `None` (not `0`) on failure."""
    try:
        query = supabase.table(table).select("*", count="exact")
        for field, value in (filters or {}).items():
            query = query.eq(field, value)
        if gte:
            query = query.gte(*gte)
        return query.execute().count
    except Exception:
        return None


@router.get("/daily-briefing")
async def founder_daily_briefing(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")

    now = datetime.now(timezone.utc)
    today = date.today()
    yesterday = today - timedelta(days=1)
    today_start = today.isoformat()
    yesterday_start = yesterday.isoformat()

    # --- 1. Business -----------------------------------------------------------
    affiliate_revenue_today = None
    try:
        events = (
            supabase.table(AFFILIATE_EVENT_TABLE)
            .select("revenue")
            .eq("event_type", "conversion")
            .gte("created_at", today_start)
            .execute()
            .data
            or []
        )
        affiliate_revenue_today = sum(float(row.get("revenue") or 0) for row in events)
    except Exception:
        affiliate_revenue_today = None

    business = {
        "revenue_today": None,
        "revenue_today_note": NO_STRIPE_REPORTING_NOTE,
        "revenue_yesterday": None,
        "revenue_yesterday_note": NO_STRIPE_REPORTING_NOTE,
        "revenue_month": None,
        "revenue_month_note": NO_STRIPE_REPORTING_NOTE,
        "premium_sales": None,
        "premium_sales_note": NO_PREMIUM_TIMESTAMP_NOTE,
        "affiliate_revenue_today": affiliate_revenue_today,
    }

    # --- 2. Nutzer ---------------------------------------------------------------
    new_users_today = _count(USER_TABLE, gte=("created_at", today_start))
    new_users_yesterday = _count(USER_TABLE, gte=("created_at", yesterday_start))
    if new_users_yesterday is not None and new_users_today is not None:
        # gte("created_at", yesterday_start) includes today too, so isolate yesterday-only.
        new_users_yesterday_only: int | None = max(new_users_yesterday - new_users_today, 0)
    else:
        new_users_yesterday_only = None

    active_users_today = None
    try:
        active_rows = (
            supabase.table(DAILY_ENTRY_TABLE).select("email").eq("entry_date", today_start).execute().data or []
        )
        active_users_today = len({row["email"] for row in active_rows if row.get("email")})
    except Exception:
        active_users_today = None

    users = {
        "new_today": new_users_today,
        "active_today": active_users_today,
        "new_premium": None,
        "new_premium_note": NO_PREMIUM_TIMESTAMP_NOTE,
        "cancellations": None,
        "cancellations_note": NO_CANCELLATION_NOTE,
    }

    # --- 3. KI ---------------------------------------------------------------------
    ai_requests_today = None
    try:
        usage_rows = supabase.table(CHAT_USAGE_TABLE).select("count").eq("usage_date", today_start).execute().data or []
        ai_requests_today = sum(int(row.get("count", 0)) for row in usage_rows)
    except Exception:
        ai_requests_today = None

    ai = {
        "requests_today": ai_requests_today,
        "cost": None,
        "cost_note": NO_COST_NOTE,
        "errors": None,
        "errors_note": NO_ERROR_TRACKING_NOTE,
        "slow_responses": None,
        "slow_responses_note": NO_LATENCY_NOTE,
    }

    # --- 4. Affiliate ----------------------------------------------------------------
    new_products_today = _count(AFFILIATE_PRODUCT_TABLE, gte=("created_at", today_start))
    pending_approval = _count(AFFILIATE_PRODUCT_TABLE, filters={"status": "in_review"})
    broken_links = _count(AFFILIATE_PRODUCT_TABLE, filters={"link_status": "broken"})

    top_products: list[dict] = []
    try:
        conversions = (
            supabase.table(AFFILIATE_EVENT_TABLE).select("product_id,revenue").eq("event_type", "conversion").execute().data
            or []
        )
        revenue_by_product: dict[str, float] = {}
        for row in conversions:
            pid = str(row.get("product_id"))
            revenue_by_product[pid] = revenue_by_product.get(pid, 0.0) + float(row.get("revenue") or 0)
        if revenue_by_product:
            product_rows = supabase.table(AFFILIATE_PRODUCT_TABLE).select("id,title").execute().data or []
            title_by_id = {str(p["id"]): p.get("title", "—") for p in product_rows}
            top_products = [
                {"product_id": pid, "title": title_by_id.get(pid, "—"), "revenue": revenue}
                for pid, revenue in sorted(revenue_by_product.items(), key=lambda kv: kv[1], reverse=True)[:3]
            ]
    except Exception:
        top_products = []

    affiliate = {
        "new_products_today": new_products_today,
        "pending_approval": pending_approval,
        "broken_links": broken_links,
        "top_products": top_products,
    }

    # --- 5. System -------------------------------------------------------------------
    database_reachable = _count(USER_TABLE) is not None
    system = {
        "server_status": None,
        "server_status_note": NO_SERVER_MONITORING_NOTE,
        "database": "reachable" if database_reachable else "unreachable",
        "api_status": "online",
        "build_status": None,
        "build_status_note": NO_BUILD_STATUS_NOTE,
        "backups": None,
        "backups_note": NO_BACKUP_MONITORING_NOTE,
    }

    # --- 6. Aufgaben (automatisch generiert) ------------------------------------------
    new_feedback_today = _count(FEEDBACK_TABLE, gte=("created_at", today_start))
    tasks = [
        {"label": "Produkte prüfen", "value": pending_approval, "note": None},
        {"label": "Releases prüfen", "value": None, "note": NO_RELEASE_TRACKING_NOTE},
        {"label": "Bugs prüfen", "value": None, "note": NO_BUG_TRACKING_NOTE},
        {"label": "Support prüfen", "value": new_feedback_today, "note": None},
        {"label": "Dokumentation prüfen", "value": None, "note": NO_DOC_FRESHNESS_NOTE},
    ]

    # --- 7. Warnungen (nur bei echtem Handlungsbedarf, keine Spam-Meldungen) ----------
    warnings: list[str] = []
    if database_reachable is False:
        warnings.append("Datenbank ist nicht erreichbar.")
    if broken_links:
        warnings.append(f"{broken_links} Affiliate-Link(s) sind defekt und sollten geprüft werden.")
    if pending_approval and pending_approval >= 5:
        warnings.append(f"{pending_approval} Affiliate-Produkte warten auf Freigabe — ungewöhnlich viele.")

    # --- 8. KI-Empfehlungen (regelbasiert, kein LLM-Aufruf) ---------------------------
    recommendations: list[dict] = []
    if new_products_today:
        recommendations.append(
            {
                "text": f"Es wurden {new_products_today} neue Affiliate-Produkt(e) gefunden.",
                "reason": f"{new_products_today} Produkt(e) mit Erstellungsdatum heute ({today_start}).",
            }
        )
    if broken_links:
        recommendations.append(
            {
                "text": f"{broken_links} Link(s) funktionieren nicht.",
                "reason": f"{broken_links} Produkt(e) mit link_status='broken' (zuletzt geprüft per Link-Check).",
            }
        )
    if new_users_yesterday_only is not None and new_users_yesterday_only > 0 and new_users_today is not None:
        change_pct = round((new_users_today - new_users_yesterday_only) / new_users_yesterday_only * 100)
        direction = "gestiegen" if change_pct >= 0 else "gesunken"
        recommendations.append(
            {
                "text": f"Neue Nutzer sind um {abs(change_pct)}% {direction}.",
                "reason": f"Heute: {new_users_today} neue Nutzer, gestern: {new_users_yesterday_only}.",
            }
        )

    # --- 9. CEO-Prioritäten (regelbasierte Einstufung, kein LLM-Aufruf) ---------------
    priorities: list[dict] = []
    if database_reachable is False:
        priorities.append({"label": "Datenbank-Erreichbarkeit prüfen", "priority": "hoch"})
    if broken_links:
        priorities.append({"label": "Defekte Affiliate-Links beheben", "priority": "hoch"})
    if pending_approval:
        priorities.append({"label": "Affiliate-Produkte zur Freigabe prüfen", "priority": "mittel"})
    if new_feedback_today:
        priorities.append({"label": "Neue Support-Anfragen prüfen", "priority": "mittel"})
    if new_products_today:
        priorities.append({"label": "Neue Affiliate-Produkte sichten", "priority": "niedrig"})
    if not priorities:
        priorities.append({"label": "Keine dringenden Prioritäten", "priority": "niedrig"})

    return {
        "generated_at": now.isoformat(),
        "business": business,
        "users": users,
        "ai": ai,
        "affiliate": affiliate,
        "system": system,
        "tasks": tasks,
        "warnings": warnings,
        "recommendations": recommendations,
        "priorities": priorities,
        "automation": _automation_summary(),
        "ceo_summary": _ceo_summary(),
        "documentation": _documentation_summary(),
    }


def _documentation_summary() -> dict:
    """Submodul I (Auto Documentation) integration — a small, additive
    read of core/documentation_score.py. Never raises: if the Auto
    Documentation tables don't exist yet, the briefing must still work."""
    try:
        score = documentation_score.compute_documentation_score()
        return {
            "coverage_percentage": score.get("overall_percentage"),
            "stale_documents": score.get("stale_documents"),
            "open_change_proposals": score.get("open_change_proposals"),
        }
    except Exception:
        return {"coverage_percentage": None, "stale_documents": None, "open_change_proposals": None, "note": "Auto Documentation noch nicht verfügbar."}


def _ceo_summary() -> dict:
    """Submodul H (CEO Intelligence) integration — a small, additive read
    of `core/executive_summary.py::get_ceo_daily_briefing_snippet()`.
    Never raises: if CEO Intelligence tables don't exist yet, the
    briefing must still work."""
    try:
        return executive_summary.get_ceo_daily_briefing_snippet()
    except Exception:
        return {
            "top_metric": None, "biggest_risk": None, "biggest_opportunity": None,
            "at_risk_goal": None, "open_decision_count": None, "automation_status": None,
            "note": "CEO Intelligence noch nicht verfügbar.",
        }


def _automation_summary() -> dict:
    """Submodul G (Automation Engine) integration — a small, additive read
    of `core/automation_engine.py::get_daily_briefing_summary()`. Never
    raises: if the Automation Engine tables don't exist yet (migration not
    yet run), the briefing must still work."""
    try:
        return automation_engine.get_daily_briefing_summary()
    except Exception:
        return {
            "auto_completed_today": None, "failed_today": None,
            "awaiting_approval": None, "important_warnings": None,
            "note": "Automation Engine noch nicht verfügbar.",
        }
