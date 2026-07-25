"""CEO Intelligence — Strategic Goals & Forecast (VitalTwin Enterprise,
Founder Operating System, Submodule H).

**Reuses `vt_founder_business_goals` directly** (Submodule E) — no
parallel `ExecutiveGoal` table. "Strategic Goals" in CEO Intelligence is
the same underlying data, exposed through this module's own forecast
logic and the CEO-Intelligence-specific permission surface
(`view_ceo_intelligence`/`manage_ceo_intelligence` instead of
`view_founder_os`/`manage_founder_os`).

**Forecast, honestly bounded.** `forecast_goal()` computes a real current
pace from `(current_value - start_value) / days_elapsed` and a required
pace from `(target_value - current_value) / days_remaining` — never a
guarantee. Uses the "Bei gleichbleibender Entwicklung könnte …" framing
from the spec, and returns `None`/an honest note whenever the underlying
dates or values are missing.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import founder_business_goals as goals_module
from .supabase import supabase

GOAL_TABLE = "vt_founder_business_goals"


def list_strategic_goals() -> list[dict]:
    try:
        goals = supabase.table(GOAL_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        goals = []
    for goal in goals:
        current_value, note = goals_module.compute_goal_progress(goal)
        goal["current_progress"] = current_value
        goal["progress_note"] = note
        goal["explanation"] = goals_module.explain_goal_progress(goal, current_value)
        goal["forecast"] = forecast_goal(goal, current_value=current_value)
    return goals


def forecast_goal(goal: dict, *, current_value: float | None = None) -> dict:
    if current_value is None:
        current_value, _note = goals_module.compute_goal_progress(goal)

    target_value = goal.get("target_value")
    start_value = goal.get("start_value")
    start_date_raw = goal.get("start_date")
    target_date_raw = goal.get("target_date")

    if current_value is None or target_value is None or start_value is None or not start_date_raw:
        return {
            "computable": False,
            "note": "Nicht genügend Datenbasis für eine Prognose (Startwert, Zielwert oder aktueller Wert fehlt).",
            "current_pace_per_day": None, "required_pace_per_day": None,
            "estimated_completion_date": None, "uncertainty": None, "statement": None,
        }

    try:
        start = date.fromisoformat(str(start_date_raw))
    except (ValueError, TypeError):
        return {
            "computable": False, "note": "Startdatum ungültig oder nicht gesetzt.",
            "current_pace_per_day": None, "required_pace_per_day": None,
            "estimated_completion_date": None, "uncertainty": None, "statement": None,
        }

    today = date.today()
    days_elapsed = max((today - start).days, 1)
    current_pace = (current_value - start_value) / days_elapsed

    required_pace = None
    if target_date_raw:
        try:
            target_date = date.fromisoformat(str(target_date_raw))
            days_remaining = max((target_date - today).days, 1)
            required_pace = (target_value - current_value) / days_remaining
        except (ValueError, TypeError):
            required_pace = None

    estimated_completion_date = None
    if current_pace > 0 and current_value < target_value:
        days_needed = (target_value - current_value) / current_pace
        estimated_completion_date = (today + timedelta(days=round(days_needed))).isoformat()
    elif current_value >= target_value:
        estimated_completion_date = today.isoformat()

    uncertainty = "hoch" if days_elapsed < 14 else "mittel" if days_elapsed < 30 else "gering"

    if current_pace <= 0:
        statement = "Bei gleichbleibender Entwicklung ist aktuell kein Fortschritt in Richtung Ziel erkennbar."
    elif estimated_completion_date:
        statement = f"Bei gleichbleibender Entwicklung könnte das Ziel etwa am {estimated_completion_date} erreicht werden."
    else:
        statement = "Bei gleichbleibender Entwicklung ist der Zielwert bereits erreicht oder überschritten."

    return {
        "computable": True,
        "note": f"Berechnet aus {days_elapsed} Tagen Datenbasis seit Zielstart — Unsicherheit: {uncertainty}.",
        "current_pace_per_day": round(current_pace, 4),
        "required_pace_per_day": round(required_pace, 4) if required_pace is not None else None,
        "estimated_completion_date": estimated_completion_date,
        "uncertainty": uncertainty,
        "statement": statement,
    }
