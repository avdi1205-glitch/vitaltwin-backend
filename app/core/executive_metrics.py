"""CEO Intelligence — Executive Metrics aggregation (VitalTwin Enterprise,
Founder Operating System, Submodule H).

**Aggregation layer, not a new data source.** Every number here is either
computed by an existing Founder-OS module (`founder_business_metrics.py`,
`automation_score.py`, `affiliate_provider.py`, `core.integrations.py`)
and re-exposed with a richer, CEO-facing shape (`value`, `source`,
`period`, `comparison_period`, `computed_at`, `trend`, `data_quality`), or
— where no such module exists yet — computed directly from the same raw
tables using the same "small-group-guard" privacy rule.

**No individual Wellness/CGM/Nutrition/Sleep/Movement/Twin-Memory data.**
Only aggregated, anonymized business/system numbers, exactly like every
other Founder-OS submodule.

**Honesty over completeness.** Every metric listed in the spec that has
no real, computable data source in this codebase (Customer Acquisition
Cost — no ad-spend tracking; Lifetime Value — no per-user revenue
history; API latency/uptime — no APM; build/release status — no CI/CD
integration; feature-usage funnel — no event-level product analytics)
returns `value: None` with an honest `data_quality: "nicht_verbunden"`
and an explanatory note — never a fabricated number.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from . import automation_score as automation_score_module
from . import founder_business_metrics as metrics
from .concurrency import run_parallel
from .integrations import get_full_integration_report
from .supabase import supabase

DataQuality = Literal["vollstaendig", "teilweise", "veraltet", "nicht_verbunden", "widersprüchlich", "unzureichend"]

DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
AFFILIATE_EVENT_TABLE = "vt_affiliate_events"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
AFFILIATE_PARTNER_TABLE = "vt_affiliate_partners"

NO_CAC_NOTE = "Kein Werbekosten-Tracking implementiert — Customer Acquisition Cost nicht berechenbar."
NO_LTV_NOTE = "Keine Umsatz-Historie pro Nutzer gespeichert — Lifetime Value nicht berechenbar."
NO_CHURN_NOTE = "Keine Kündigungs-/Downgrade-Erfassung implementiert (siehe founder_business_metrics.NO_CANCELLATION_NOTE)."
NO_FUNNEL_NOTE = "Kein Event-Level-Produkt-Tracking implementiert — Aktivierungsfunnel/Abbruchpunkte nicht berechenbar."
NO_APM_NOTE = "Keine APM-/Uptime-Integration vorhanden — Uptime, Fehlerquote, API-Latenz nicht berechenbar."
NO_RELEASE_NOTE = "Kein CI/CD-/Release-Tracking implementiert — Build-/Release-Status nicht berechenbar."
NO_BACKUP_NOTE = "Keine Backup-Monitoring-Integration vorhanden (Supabase verwaltet Backups selbst)."
NO_BUG_TRACKING_NOTE = "Kein Bug-Tracking-System integriert — offene kritische Bugs nicht zählbar."


def _metric(value, *, source: str, period: str | None = None, comparison_period: str | None = None,
            trend: float | None = None, data_quality: str = "vollstaendig", note: str | None = None) -> dict:
    return {
        "value": value,
        "source": source,
        "period": period,
        "comparison_period": comparison_period,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "trend": trend,
        "data_quality": data_quality if value is not None else "nicht_verbunden",
        "note": note,
    }


def _pct_trend(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _active_users_7d() -> int | None:
    try:
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        rows = supabase.table(DAILY_ENTRY_TABLE).select("email").gte("entry_date", week_ago).execute().data or []
        return len({r["email"] for r in rows if r.get("email")})
    except Exception:
        return None


def _total_registrations() -> int | None:
    return metrics.count_rows("vt_users")


def _affiliate_impressions_clicks(days: int = 7) -> tuple[int | None, int | None]:
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        events = supabase.table(AFFILIATE_EVENT_TABLE).select("event_type").gte("created_at", since).execute().data or []
    except Exception:
        return None, None
    impressions = sum(1 for e in events if e.get("event_type") == "impression")
    clicks = sum(1 for e in events if e.get("event_type") == "click")
    return impressions, clicks


# ---------------------------------------------------------------------------
# CEO Overview
# ---------------------------------------------------------------------------


def get_ceo_overview() -> dict:
    def _open_tasks_rows() -> list[dict]:
        try:
            return supabase.table("vt_founder_tasks").select("status").execute().data or []
        except Exception:
            return []

    def _approvals_rows() -> list[dict]:
        try:
            return supabase.table("vt_founder_approvals").select("status,priority").execute().data or []
        except Exception:
            return []

    def _insights_rows() -> list[dict]:
        try:
            return supabase.table("vt_founder_business_insights").select("category,status").execute().data or []
        except Exception:
            return []

    # All 8 lookups below are independent — run them concurrently instead
    # of one after another (several of them are themselves already
    # internally parallel, e.g. get_business_dashboard/compute_automation_score).
    (
        dashboard,
        (this_week_users, previous_week_users),
        active_users,
        total_users,
        premium_users,
        automation,
        open_tasks_rows,
        approvals,
        insights,
        documentation_health,
    ) = run_parallel(
        metrics.get_business_dashboard,
        metrics.get_weekly_new_users,
        _active_users_7d,
        _total_registrations,
        lambda: metrics.count_rows("vt_users", filters={"premium": True}),
        automation_score_module.compute_automation_score,
        _open_tasks_rows,
        _approvals_rows,
        _insights_rows,
        _documentation_health_metric,
    )

    open_tasks = len([t for t in open_tasks_rows if t.get("status") in ("neu", "in_bearbeitung", "warten")])
    open_approvals = len([a for a in approvals if a.get("status") in ("neu", "ki_geprueft", "zur_pruefung")])

    open_insight_statuses = {"erkannt", "geprueft", "zur_freigabe_gesendet"}
    open_risks = len([i for i in insights if "risiko" in (i.get("category") or "") and i.get("status") in open_insight_statuses])
    open_opportunities = len([i for i in insights if "chance" in (i.get("category") or "") and i.get("status") in open_insight_statuses])

    period = f"{date.today().isoformat()}"
    week_label = f"letzte 7 Tage ({(date.today() - timedelta(days=7)).isoformat()} – {date.today().isoformat()})"

    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "revenue_today": {**dashboard["revenue_today"], "period": period, "comparison_period": None, "trend": None},
        "revenue_month": {**dashboard["revenue_month"], "period": date.today().replace(day=1).isoformat(), "comparison_period": None, "trend": None},
        "mrr": {**dashboard["mrr"], "period": period, "comparison_period": None, "trend": None},
        "annual_revenue_forecast": _metric(None, source="Stripe (nicht verbunden)", note="Keine ausreichende Umsatzbasis für eine Jahresprognose vorhanden."),
        "new_users": _metric(this_week_users, source="vt_users", period=week_label, trend=_pct_trend(this_week_users, previous_week_users)),
        "active_users": _metric(active_users, source="vt_daily_wellness_entries", period=week_label),
        "premium_users": _metric(premium_users, source="vt_users", period=period),
        "new_premium_subscriptions": {**dashboard["new_premium_subscriptions"], "period": period},
        "cancellations": {**dashboard["cancellations"], "period": period},
        "affiliate_revenue": _metric(dashboard["affiliate_revenue_today"]["value"], source="vt_affiliate_events", period=period),
        "ai_cost": {**dashboard["ai_cost"], "period": period},
        "infra_cost": {**dashboard["infra_cost"], "period": period},
        "net_development": _metric(None, source="abgeleitet", note="Nicht sauber berechenbar — Umsatz, KI-Kosten und Infrastrukturkosten sind nicht vollständig verbunden."),
        "open_critical_risks": _metric(open_risks, source="vt_founder_business_insights"),
        "open_opportunities": _metric(open_opportunities, source="vt_founder_business_insights"),
        "open_founder_decisions": _metric(open_tasks + open_approvals, source="vt_founder_tasks + vt_founder_approvals"),
        "automation_percentage": _metric(automation.get("overall_percentage"), source="vt_automation_runs", note=automation.get("note")),
        "product_status": _metric(None, source="nicht verbunden", note=NO_RELEASE_NOTE),
        "release_status": _metric(None, source="nicht verbunden", note=NO_RELEASE_NOTE),
        "documentation_health": documentation_health,
    }


def _documentation_health_metric() -> dict:
    """Submodul I (Auto Documentation) integration — additive, read-only.
    Never raises: if Auto Documentation tables don't exist yet, CEO
    Intelligence must still work."""
    try:
        from . import documentation_score
        score = documentation_score.compute_documentation_score()
        return _metric(score.get("overall_percentage"), source="core/documentation_score.py", note=score.get("note"))
    except Exception:
        return _metric(None, source="core/documentation_score.py", note="Auto Documentation noch nicht verfügbar.")


# ---------------------------------------------------------------------------
# Strategic KPI System (grouped, per spec's 8 sections)
# ---------------------------------------------------------------------------


def get_strategic_kpis() -> dict:
    def _conversions_7d() -> int:
        try:
            return len(
                [e for e in (supabase.table(AFFILIATE_EVENT_TABLE).select("event_type,revenue,commission").gte(
                    "created_at", (date.today() - timedelta(days=7)).isoformat()).execute().data or [])
                 if e.get("event_type") == "conversion"]
            )
        except Exception:
            return 0

    def _broken_links() -> int:
        try:
            return metrics.count_rows(AFFILIATE_PRODUCT_TABLE, filters={"link_status": "broken"}) or 0
        except Exception:
            return 0

    # All 9 lookups below are independent — run them concurrently instead
    # of one after another.
    (
        dashboard,
        (this_week_users, previous_week_users),
        automation,
        (impressions_7d, clicks_7d),
        conversions_7d,
        broken_links,
        integration_report,
        total_registrations,
        active_users_7d,
        affiliate_revenue_by_category,
        weekly_feedback_counts,
    ) = run_parallel(
        metrics.get_business_dashboard,
        metrics.get_weekly_new_users,
        automation_score_module.compute_automation_score,
        lambda: _affiliate_impressions_clicks(7),
        _conversions_7d,
        _broken_links,
        get_full_integration_report,
        _total_registrations,
        _active_users_7d,
        lambda: metrics.get_affiliate_revenue_by_category(days=7),
        metrics.get_weekly_feedback_counts,
    )
    ctr = round(clicks_7d / impressions_7d * 100, 2) if impressions_7d else None
    ai_providers_configured = sum(1 for p in integration_report.get("ai_providers", []) if p.get("status") == "configured")

    return {
        "nutzer": {
            "gesamtregistrierungen": _metric(total_registrations, source="vt_users"),
            "neue_nutzer_7d": _metric(this_week_users, source="vt_users", trend=_pct_trend(this_week_users, previous_week_users)),
            "aktive_nutzer_7d": _metric(active_users_7d, source="vt_daily_wellness_entries"),
            "taeglich_aktive_nutzer": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "monatlich_aktive_nutzer": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "aktivierungsrate": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "retention": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "churn": _metric(None, source="nicht verbunden", note=NO_CHURN_NOTE),
            "reaktivierungen": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
        },
        "business": {
            "monatsumsatz": {**dashboard["revenue_month"]},
            "wiederkehrender_umsatz": {**dashboard["mrr"]},
            "arpu": _metric(None, source="nicht verbunden", note="Kein Umsatz pro Nutzer berechenbar (siehe MRR-Hinweis)."),
            "ltv": _metric(None, source="nicht verbunden", note=NO_LTV_NOTE),
            "cac": _metric(None, source="nicht verbunden", note=NO_CAC_NOTE),
            "rueckerstattungen": {**dashboard["cancellations"], "note": "Kein Rückerstattungs-Tracking implementiert."},
            "fehlgeschlagene_zahlungen": _metric(None, source="nicht verbunden", note="Kein Stripe-Webhook für fehlgeschlagene Zahlungen implementiert."),
            "offene_forderungen": _metric(None, source="nicht verbunden", note="Keine Rechnungsstellung mit offenen Posten implementiert."),
        },
        "premium": {
            "conversion_rate": {**dashboard["conversion_rate"]},
            "monatsabo": _metric(None, source="nicht verbunden", note="Kein Plan-/Abrechnungszyklus pro Nutzer gespeichert."),
            "jahresabo": _metric(None, source="nicht verbunden", note="Kein Plan-/Abrechnungszyklus pro Nutzer gespeichert."),
            "tarifwechsel": _metric(None, source="nicht verbunden", note="Kein Tarifwechsel-Tracking implementiert."),
            "kuendigungen": {**dashboard["cancellations"]},
            "kuendigungsgruende": _metric(None, source="nicht verbunden", note=NO_CHURN_NOTE),
            "testphase_zu_zahlend": _metric(None, source="nicht verbunden", note="Keine Testphasen-Erfassung implementiert."),
        },
        "affiliate": {
            "impressionen_7d": _metric(impressions_7d, source="vt_affiliate_events"),
            "klicks_7d": _metric(clicks_7d, source="vt_affiliate_events"),
            "ctr": _metric(ctr, source="vt_affiliate_events"),
            "verkaeufe_7d": _metric(conversions_7d, source="vt_affiliate_events"),
            "conversion": _metric(round(conversions_7d / clicks_7d * 100, 2) if clicks_7d else None, source="vt_affiliate_events"),
            "umsatz_nach_kategorie": _metric(affiliate_revenue_by_category, source="vt_affiliate_events × vt_affiliate_products × vt_affiliate_categories"),
            "defekte_links": _metric(broken_links, source="vt_affiliate_products"),
        },
        "ki": {
            "requests": _metric(None, source="nicht verbunden", note="Kein Request-Zähler für den AI Provider implementiert."),
            "tokenverbrauch": {**dashboard["ai_cost"]},
            "kosten": {**dashboard["ai_cost"]},
            "kosten_pro_nutzer": _metric(None, source="nicht verbunden", note=metrics.NO_COST_NOTE),
            "kosten_pro_premium_nutzer": _metric(None, source="nicht verbunden", note=metrics.NO_COST_NOTE),
            "fehlerquote": _metric(None, source="nicht verbunden", note="Keine Fehlerquote-Messung implementiert."),
            "antwortzeit": _metric(None, source="nicht verbunden", note=NO_APM_NOTE),
            "provider_verteilung": _metric({"openai": ai_providers_configured}, source="core/integrations.py::get_ai_providers()"),
        },
        "produkt": {
            "feature_nutzung": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "aktivierungsfunnel": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "abbruchpunkte": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "meistgenutzte_funktionen": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "wenig_genutzte_funktionen": _metric(None, source="nicht verbunden", note=NO_FUNNEL_NOTE),
            "supportsignale": _metric(weekly_feedback_counts[0], source="vt_user_feedback"),
        },
        "technik": {
            "uptime": _metric(None, source="nicht verbunden", note=NO_APM_NOTE),
            "fehlerquote": _metric(None, source="nicht verbunden", note=NO_APM_NOTE),
            "api_latenz": _metric(None, source="nicht verbunden", note=NO_APM_NOTE),
            "build_status": _metric(None, source="nicht verbunden", note=NO_RELEASE_NOTE),
            "backup_status": _metric(None, source="nicht verbunden", note=NO_BACKUP_NOTE),
            "offene_kritische_bugs": _metric(None, source="nicht verbunden", note=NO_BUG_TRACKING_NOTE),
            "release_readiness": _metric(None, source="nicht verbunden", note=NO_RELEASE_NOTE),
        },
        "automatisierung": {
            "automatisch_erledigte_aufgaben": _metric(automation.get("automated_runs_30d"), source="vt_automation_runs"),
            "manuelle_aufgaben": _metric(automation.get("manual_decisions_30d"), source="vt_founder_tasks + vt_founder_approvals"),
            "wartende_freigaben": _metric(None, source="siehe vt_automation_runs (status='wartet_auf_freigabe')"),
            "fehler_in_automationen": _metric(automation.get("failed_runs_30d"), source="vt_automation_runs"),
            "eingesparte_prozessschritte": _metric(automation.get("automated_runs_30d"), source="vt_automation_runs", note="Näherung: Anzahl erfolgreicher automatisierter Läufe."),
            "automation_score": _metric(automation.get("overall_percentage"), source="core/automation_score.py"),
        },
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
