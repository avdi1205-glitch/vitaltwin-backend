"""CEO Intelligence — Scenario Planning (VitalTwin Enterprise, Founder
Operating System, Submodule H).

Pure, transparent what-if calculations over real current baselines —
**never a price change, never a guarantee**. Each scenario function
returns its exact assumption, the real baseline it started from, the
projected effect, an honest uncertainty note, and — where the underlying
data genuinely does not exist (churn rate, AI cost, annual-vs-monthly
plan split) — `computable: False` with a clear reason instead of a
fabricated number.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import founder_business_metrics as metrics
from .supabase import supabase

SCENARIO_TABLE = "vt_executive_scenarios"

SCENARIO_TYPES = frozenset({
    "premium_conversion_up", "churn_down", "affiliate_ctr_up", "ai_cost_up", "new_users_grow", "annual_plan_share_up",
})

UNCERTAINTY_NOTE = "Einfache lineare Schätzung auf Basis aktueller Kennzahlen — keine Garantie, keine automatische Umsetzung."


def _not_computable(reason: str) -> dict:
    return {"computable": False, "reason": reason, "baseline": None, "projected": None, "affected_metrics": [], "uncertainty_note": None, "limits_note": reason}


def simulate_premium_conversion_up(delta_pct: float) -> dict:
    total_users = metrics.count_rows("vt_users")
    premium_users = metrics.count_rows("vt_users", filters={"premium": True})
    if not total_users or premium_users is None:
        return _not_computable("Keine Nutzerdaten vorhanden.")
    current_rate = premium_users / total_users
    projected_rate = min(current_rate * (1 + delta_pct / 100), 1.0)
    projected_premium_users = round(total_users * projected_rate)
    return {
        "computable": True,
        "baseline": {"conversion_rate": round(current_rate, 4), "premium_users": premium_users, "total_users": total_users},
        "projected": {"conversion_rate": round(projected_rate, 4), "premium_users": projected_premium_users, "additional_premium_users": projected_premium_users - premium_users},
        "affected_metrics": ["conversion_rate", "premium_users"],
        "uncertainty_note": UNCERTAINTY_NOTE,
        "limits_note": "Umsatzauswirkung nicht berechenbar, da kein Plan-/Preis-Feld pro Nutzer gespeichert ist (nur Nutzerzahl-Effekt).",
    }


def simulate_churn_down(delta_pct: float) -> dict:
    return _not_computable(
        "Keine Kündigungs-/Downgrade-Erfassung implementiert (Stripe-Webhook behandelt nur checkout.session.completed) "
        "— keine Baseline-Kündigungsrate zum Simulieren vorhanden."
    )


def simulate_affiliate_ctr_up(delta_pct: float) -> dict:
    since = (date.today() - timedelta(days=7)).isoformat()
    try:
        events = supabase.table("vt_affiliate_events").select("event_type,revenue,commission").gte("created_at", since).execute().data or []
    except Exception:
        events = []
    impressions = sum(1 for e in events if e.get("event_type") == "impression")
    clicks = sum(1 for e in events if e.get("event_type") == "click")
    conversions = [e for e in events if e.get("event_type") == "conversion"]
    if not impressions or not clicks:
        return _not_computable("Keine ausreichenden Affiliate-Impressionen/-Klicks in den letzten 7 Tagen vorhanden.")

    current_ctr = clicks / impressions
    projected_ctr = min(current_ctr * (1 + delta_pct / 100), 1.0)
    projected_clicks = round(impressions * projected_ctr)
    conversion_rate = len(conversions) / clicks if clicks else 0
    avg_commission = sum(float(c.get("commission") or 0) for c in conversions) / len(conversions) if conversions else 0
    projected_conversions = round(projected_clicks * conversion_rate)
    projected_commission = round(projected_conversions * avg_commission, 2)
    current_commission = round(sum(float(c.get("commission") or 0) for c in conversions), 2)

    return {
        "computable": True,
        "baseline": {"ctr": round(current_ctr, 4), "clicks_7d": clicks, "conversions_7d": len(conversions), "commission_7d": current_commission},
        "projected": {"ctr": round(projected_ctr, 4), "clicks_7d": projected_clicks, "conversions_7d": projected_conversions, "commission_7d": projected_commission},
        "affected_metrics": ["ctr", "clicks", "conversions", "commission"],
        "uncertainty_note": UNCERTAINTY_NOTE + " Setzt eine unveränderte Conversion-Rate und Durchschnittsprovision voraus.",
        "limits_note": "Reine Hochrechnung auf Basis der letzten 7 Tage, keine saisonale Anpassung.",
    }


def simulate_ai_cost_up(delta_pct: float) -> dict:
    return _not_computable(
        "Kein Kosten-Tracking implementiert (services/ai_provider.py gibt keinen Token-/Kostenverbrauch zurück) "
        "— keine Baseline-KI-Kosten zum Simulieren vorhanden."
    )


def simulate_new_users_grow(delta_pct: float) -> dict:
    this_week, previous_week = metrics.get_weekly_new_users()
    if this_week is None:
        return _not_computable("Keine Registrierungsdaten vorhanden.")
    projected_new_users = round(this_week * (1 + delta_pct / 100))
    total_users = metrics.count_rows("vt_users")
    premium_users = metrics.count_rows("vt_users", filters={"premium": True})
    conversion_rate = (premium_users / total_users) if total_users and premium_users is not None else None
    estimated_additional_premium = round((projected_new_users - this_week) * conversion_rate) if conversion_rate is not None else None
    return {
        "computable": True,
        "baseline": {"new_users_7d": this_week, "previous_week": previous_week},
        "projected": {"new_users_7d": projected_new_users, "estimated_additional_premium_users": estimated_additional_premium},
        "affected_metrics": ["new_users", "premium_users (geschätzt)"],
        "uncertainty_note": UNCERTAINTY_NOTE + " Geschätzte Premium-Nutzer setzen eine unveränderte Conversion-Rate voraus.",
        "limits_note": "Keine Kanalzuordnung — Wachstum wird pauschal angenommen.",
    }


def simulate_annual_plan_share_up(delta_pct: float) -> dict:
    return _not_computable(
        "Kein Plan-/Abrechnungszyklus (Monats-/Jahresabo) pro Nutzer gespeichert — kein Anteil zum Simulieren vorhanden."
    )


_SIMULATORS = {
    "premium_conversion_up": simulate_premium_conversion_up,
    "churn_down": simulate_churn_down,
    "affiliate_ctr_up": simulate_affiliate_ctr_up,
    "ai_cost_up": simulate_ai_cost_up,
    "new_users_grow": simulate_new_users_grow,
    "annual_plan_share_up": simulate_annual_plan_share_up,
}


def run_scenario(scenario_type: str, *, delta_pct: float) -> dict:
    if scenario_type not in SCENARIO_TYPES:
        raise ValueError(f"Unbekannter Szenario-Typ. Erlaubt: {', '.join(sorted(SCENARIO_TYPES))}")
    result = _SIMULATORS[scenario_type](delta_pct)
    return {"scenario_type": scenario_type, "assumption": {"delta_pct": delta_pct}, **result}


def save_scenario(*, name: str, scenario_type: str, delta_pct: float, created_by: str) -> dict:
    result = run_scenario(scenario_type, delta_pct=delta_pct)
    payload = {
        "name": name, "scenario_type": scenario_type,
        "assumptions": {"delta_pct": delta_pct}, "results": result,
        "computable": result["computable"], "created_by": created_by,
    }
    response = supabase.table(SCENARIO_TABLE).insert(payload).execute()
    return response.data[0] if response.data else payload


def list_scenarios() -> list[dict]:
    return supabase.table(SCENARIO_TABLE).select("*").order("created_at", desc=True).execute().data or []


def delete_scenario(scenario_id: str) -> None:
    supabase.table(SCENARIO_TABLE).delete().eq("id", scenario_id).execute()
