"""CEO Intelligence — Executive Scorecard (VitalTwin Enterprise, Founder
Operating System, Submodule H).

14 strategic dimensions, each with a rule-based status. **No
pseudo-scientific overall score** is computed (per spec, "Keine
scheinwissenschaftliche Gesamtbewertung") — each dimension stands on its
own with its own data quality and status.
"""

from __future__ import annotations

from typing import Literal

from . import automation_score as automation_score_module
from . import executive_metrics as ex_metrics
from . import founder_business_metrics as metrics

Status = Literal["sehr_gut", "im_plan", "beobachten", "gefaehrdet", "kritisch", "keine_daten"]


def _dimension(*, area: str, status: Status, trend: float | None, data_basis: str,
               target_value: float | None, current_value: float | None, deviation: float | None,
               risk_level: str, next_action: str) -> dict:
    return {
        "area": area, "status": status, "trend": trend, "data_basis": data_basis,
        "target_value": target_value, "current_value": current_value, "deviation": deviation,
        "risk_level": risk_level, "next_action": next_action,
    }


def _status_from_trend(value: float | None, *, good_if_positive: bool = True) -> Status:
    if value is None:
        return "keine_daten"
    if good_if_positive:
        if value >= 10:
            return "sehr_gut"
        if value >= 0:
            return "im_plan"
        if value >= -10:
            return "beobachten"
        return "gefaehrdet"
    if value <= -10:
        return "sehr_gut"
    if value <= 0:
        return "im_plan"
    if value <= 10:
        return "beobachten"
    return "gefaehrdet"


def compute_scorecard() -> list[dict]:
    overview = ex_metrics.get_ceo_overview()
    kpis = ex_metrics.get_strategic_kpis()
    automation = automation_score_module.compute_automation_score()

    new_users_trend = overview["new_users"]["trend"]
    growth = _dimension(
        area="Wachstum", status=_status_from_trend(new_users_trend), trend=new_users_trend,
        data_basis="vt_users.created_at, Zeitfenster-Vergleich (7 vs. 7 Tage).",
        target_value=None, current_value=overview["new_users"]["value"], deviation=None,
        risk_level="niedrig" if (new_users_trend or 0) >= 0 else "mittel",
        next_action="Kein Handlungsbedarf." if (new_users_trend or 0) >= 0 else "Registrierungsrückgang prüfen (Marketing/Saisonalität).",
    )

    revenue_status: Status = "keine_daten"
    revenue = _dimension(
        area="Umsatz", status=revenue_status, trend=None,
        data_basis="Kein Stripe-Reporting angebunden.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="Stripe-Reporting-API anbinden, um Umsatzkennzahlen verlässlich zu erhalten.",
    )

    profitability = _dimension(
        area="Profitabilität", status="keine_daten", trend=None,
        data_basis="Umsatz, KI-Kosten und Infrastrukturkosten sind nicht vollständig verbunden.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="Kostentracking (KI + Infrastruktur) und Umsatz-Reporting vervollständigen.",
    )

    conversion_value = overview["premium_users"]["value"]
    conversion_rate = kpis["premium"]["conversion_rate"]["value"]
    conversion = _dimension(
        area="Premium-Conversion", status="im_plan" if conversion_rate else "keine_daten", trend=None,
        data_basis="vt_users (premium-Flag / Gesamtnutzer).",
        target_value=None, current_value=conversion_rate, deviation=None,
        risk_level="niedrig" if (conversion_rate or 0) > 0 else "unbekannt",
        next_action="Conversion-Funnel-Tracking ergänzen, um gezielte Optimierung zu ermöglichen." if conversion_rate else "Keine Daten vorhanden.",
    )

    retention = _dimension(
        area="Nutzerbindung", status="keine_daten", trend=None,
        data_basis="Kein Event-Level-Produkt-Tracking implementiert.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="Produktnutzungs-Tracking (Sessions/Wiederkehr) einführen.",
    )

    churn = _dimension(
        area="Kündigungsrate", status="keine_daten", trend=None,
        data_basis="Keine Kündigungs-/Downgrade-Erfassung implementiert.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="Stripe-Webhook um subscription.deleted/updated erweitern.",
    )

    affiliate_ctr = kpis["affiliate"]["ctr"]["value"]
    affiliate_perf = _dimension(
        area="Affiliate-Leistung", status=_status_from_trend((affiliate_ctr or 0) - 2, good_if_positive=True) if affiliate_ctr else "keine_daten",
        trend=None, data_basis="vt_affiliate_events (Impressionen/Klicks, 7 Tage).",
        target_value=None, current_value=affiliate_ctr, deviation=None,
        risk_level="niedrig" if affiliate_ctr else "unbekannt",
        next_action="Kein akuter Handlungsbedarf." if affiliate_ctr else "Noch keine Affiliate-Events vorhanden.",
    )

    product_usage = _dimension(
        area="Produktnutzung", status="keine_daten", trend=None,
        data_basis="Kein Feature-Nutzungs-Tracking implementiert.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="Feature-Nutzungs-Events einführen.",
    )

    ai_cost_efficiency = _dimension(
        area="KI-Kosteneffizienz", status="keine_daten", trend=None,
        data_basis="Keine OpenAI-Kosten-API-Anbindung vorhanden.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="OpenAI-Nutzungs-API anbinden, um Kosten pro Nutzer sichtbar zu machen.",
    )

    integration_report_note = "core/integrations.py — konfigurierte vs. nicht konfigurierte Integrationen."
    system_stability = _dimension(
        area="Systemstabilität", status="keine_daten", trend=None,
        data_basis=f"Keine APM-/Uptime-Integration vorhanden ({integration_report_note}).",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="APM-/Uptime-Monitoring einführen (z. B. Healthchecks + Alerting).",
    )

    try:
        support_this_week, support_previous_week = metrics.get_weekly_feedback_counts()
    except Exception:
        support_this_week, support_previous_week = None, None
    support_trend = None
    if support_this_week is not None and support_previous_week:
        support_trend = round((support_this_week - support_previous_week) / support_previous_week * 100, 1)
    support_load = _dimension(
        area="Supportbelastung", status=_status_from_trend(support_trend, good_if_positive=False) if support_trend is not None else "keine_daten",
        trend=support_trend, data_basis="vt_user_feedback.created_at, Zeitfenster-Vergleich (7 vs. 7 Tage).",
        target_value=None, current_value=support_this_week, deviation=None,
        risk_level="hoch" if (support_trend or 0) > 30 else "niedrig",
        next_action="Feedback der letzten 7 Tage nach wiederkehrenden Themen sichten." if (support_trend or 0) > 30 else "Kein akuter Handlungsbedarf.",
    )

    release_quality = _dimension(
        area="Release-Qualität", status="keine_daten", trend=None,
        data_basis="Kein CI/CD-/Release-Tracking implementiert.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="Release-Tracking (z. B. Changelog + Deployment-Log) einführen.",
    )

    automation_pct = automation.get("overall_percentage")
    automation_dim = _dimension(
        area="Automatisierungsgrad", status=(
            "sehr_gut" if (automation_pct or 0) >= 70 else
            "im_plan" if (automation_pct or 0) >= 40 else
            "beobachten" if automation_pct is not None else "keine_daten"
        ), trend=automation.get("trend_vs_previous_30d"),
        data_basis="vt_automation_runs + manuell abgeschlossene Aufgaben/Freigaben (30 Tage).",
        target_value=None, current_value=automation_pct, deviation=None,
        risk_level="niedrig" if (automation_pct or 0) >= 40 else "mittel",
        next_action="Automatisierungschancen im Automation-Tab prüfen." if automation_pct is not None else "Noch keine Prozessdaten vorhanden.",
    )

    goal_achievement = _dimension(
        area="Zielerreichung", status="keine_daten", trend=None,
        data_basis="Siehe Strategic Goals (vt_founder_business_goals) — je Ziel individuell bewertet.",
        target_value=None, current_value=None, deviation=None,
        risk_level="unbekannt", next_action="Strategic Goals definieren, um Zielerreichung zu bewerten.",
    )

    return [
        growth, revenue, profitability, conversion, retention, churn, affiliate_perf,
        product_usage, ai_cost_efficiency, system_stability, support_load, release_quality,
        automation_dim, goal_achievement,
    ]
