"""AI Business Coach — Business Goal progress computation (VitalTwin
Enterprise, Founder Operating System, Submodule E).

Maps a goal's `category` to a real, currently-computable metric where one
exists. **No guarantees, no invented forecasts** — `explain_goal_progress`
only ever states what the real numbers show right now (on track / at
risk / not computable), never a projected completion date or probability.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import founder_business_metrics as metrics

USER_TABLE = "vt_users"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"


def _current_automation_percentage() -> float | None:
    # Local import to avoid a module-load-order dependency between the two
    # core modules — both are leaf modules, this just keeps import order
    # flexible for tests that import either file first.
    from . import automation_score

    try:
        return automation_score.compute_automation_score().get("overall_percentage")
    except Exception:
        return None


def _current_active_users_7d() -> int | None:
    try:
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        rows = (
            metrics.supabase.table(DAILY_ENTRY_TABLE).select("email").gte("entry_date", week_ago).execute().data or []
        )
        return len({r["email"] for r in rows if r.get("email")})
    except Exception:
        return None


def _current_conversion_rate() -> float | None:
    total = metrics.count_rows(USER_TABLE)
    premium = metrics.count_rows(USER_TABLE, filters={"premium": True})
    if not total or premium is None:
        return None
    return round(premium / total, 3)


# Maps a goal category to a function returning the current real value, or
# None if genuinely not computable.
_METRIC_RESOLVERS = {
    "aktive_nutzer": _current_active_users_7d,
    "premium_abos": lambda: metrics.count_rows(USER_TABLE, filters={"premium": True}),
    "conversion_rate": _current_conversion_rate,
    "affiliate_umsatz": lambda: sum(metrics.get_affiliate_revenue_by_category(days=30).values()),
    "veroeffentlichte_inhalte": metrics.get_published_content_count,
    # Additiv für CEO Intelligence (Submodul H): Automatisierungsgrad ist
    # über core/automation_score.py real berechenbar. "release_ziel" und
    # "internationales_wachstum" bleiben absichtlich ohne Resolver — es
    # gibt kein Release-Tracking und keine Länder-/Sprach-Aufschlüsselung
    # der Nutzer in dieser Codebase (ehrlich "nicht automatisch
    # berechenbar" statt erfunden).
    "automatisierungsziel": _current_automation_percentage,
}


def compute_goal_progress(goal: dict) -> tuple[float | None, str]:
    """Returns `(current_value_or_None, data_source_note)`."""
    category = goal.get("category")
    resolver = _METRIC_RESOLVERS.get(category)
    if resolver is None:
        return None, "Fortschritt für diese Kategorie nicht automatisch berechenbar."
    try:
        value = resolver()
    except Exception:
        return None, "Datenquelle gerade nicht erreichbar."
    if value is None:
        return None, "Zugrunde liegende Datenquelle liefert aktuell keinen Wert."
    return float(value), "Automatisch berechnet."


def explain_goal_progress(goal: dict, current_value: float | None) -> dict:
    """Honest, non-predictive explanation: on-track / at-risk / not
    computable — never a guaranteed forecast."""
    target = goal.get("target_value")
    target_date = goal.get("target_date")

    if current_value is None or target is None:
        return {
            "on_track": None,
            "at_risk": None,
            "helping_factors": [],
            "slowing_factors": [],
            "next_action": "Datenlage prüfen — Fortschritt kann aktuell nicht bewertet werden.",
        }

    start_value = goal.get("start_value") or 0
    progress_ratio = (current_value - start_value) / (target - start_value) if target != start_value else None

    time_ratio = None
    if target_date and goal.get("start_date"):
        try:
            start = date.fromisoformat(str(goal["start_date"]))
            end = date.fromisoformat(str(target_date))
            today = date.today()
            total_days = (end - start).days
            elapsed_days = (today - start).days
            if total_days > 0:
                time_ratio = max(0.0, min(1.0, elapsed_days / total_days))
        except (ValueError, TypeError):
            time_ratio = None

    at_risk = None
    on_track = None
    if progress_ratio is not None and time_ratio is not None:
        at_risk = progress_ratio < time_ratio - 0.1
        on_track = not at_risk

    helping = []
    slowing = []
    if progress_ratio is not None:
        if progress_ratio >= 1:
            helping.append("Zielwert bereits erreicht oder überschritten.")
        elif progress_ratio <= 0:
            slowing.append("Noch keine Bewegung in Richtung Zielwert erkennbar.")

    return {
        "on_track": on_track,
        "at_risk": at_risk,
        "helping_factors": helping,
        "slowing_factors": slowing,
        "next_action": (
            "Kein Handlungsbedarf erkennbar." if on_track else
            "Ursache für den Rückstand prüfen und Gegenmaßnahme im Approval Center vorschlagen." if at_risk else
            "Noch nicht genug Datenpunkte für eine verlässliche Einschätzung."
        ),
    }
