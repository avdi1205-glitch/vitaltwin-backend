"""AI Business Coach — insight detection engine (VitalTwin Enterprise,
Founder Operating System, Submodule E).

**No LLM call.** Exactly like every other Founder-OS detector
(`core/founder_task_detector.py`, `core/founder_approval_detector.py`,
`core/affiliate_engine.py`): "Insight erkannt" means a deterministic rule
over real, timestamped data — never a free-text generation. The AI
provider is used *only* for the free-text "Frag deinen Business Coach"
Q&A feature (see `routers/founder_business_coach.py::ask_business_coach`),
never for insight detection itself.

**Only 3 of the 16 requested insight categories have a real rule**,
because only these have a genuine, timestamped, comparable data source in
this codebase:

- **Wachstumschance** — neue Nutzerregistrierungen diese Woche vs. letzte
  Woche (`vt_users.created_at`).
- **Affiliate-Chance** — Affiliate-Umsatz je Kategorie diese Woche vs.
  letzte Woche (`vt_affiliate_events` × `vt_affiliate_products` ×
  `vt_affiliate_categories`).
- **Supportproblem** — Volumen an Nutzerfeedback diese Woche vs. letzte
  Woche (`vt_user_feedback.created_at`).

Umsatzchance/-risiko, Kostenrisiko, Conversion-Problem (als Trend),
Kündigungsrisiko, Premium-Chance, Produktproblem, technisches Risiko,
KI-Kostenproblem, SEO-Chance, Release-Risiko und Datenqualitätsproblem
(als *Kategorie*) haben **keine** aktive Regel — es gibt keine
Umsatz-Zeitreihe, keine Kosten-Zeitreihe, keine Feature-Nutzungs-/Funnel-
Daten, keinen SEO-Crawler, keinen Release-Tracker in diesem Codebase.
Eine erfundene Regel dafür wäre unehrlich; siehe
`frontend/docs/AI_BUSINESS_COACH.md` für die vollständige Aufstellung.

**Datenqualitätsproblem / Datenschutz als Querschnittsfunktion**: statt
einer eigenen Erkennungsregel wird jede Insight-Berechnung durch
`core/founder_business_metrics.py::_small_group_guard` geschützt — ein
Delta aus zu wenigen Datensätzen wird nie als Insight ausgegeben (siehe
`MIN_GROUP_SIZE`).

**Idempotent.** Wie in `founder_task_detector.py`: ein `dedupe_key` pro
Regel+Zeitraum, kein Duplikat, keine Reaktivierung einer bereits vom
Gründer entschiedenen (`umgesetzt`/`verworfen`/`archiviert`) Insight.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import founder_business_metrics as metrics
from .supabase import supabase

INSIGHT_TABLE = "vt_founder_business_insights"
TASK_TABLE = "vt_founder_tasks"
APPROVAL_TABLE = "vt_founder_approvals"
AUTOMATION_EVENT_TABLE = "vt_founder_automation_events"

TERMINAL_INSIGHT_STATUSES = ("umgesetzt", "verworfen", "archiviert")
SIGNIFICANT_CHANGE_PCT = 20  # Below this, a delta is noise, not an insight (no-spam rule).


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_automation_event(event_type: str, *, reference_table: str | None = None, reference_id: str | None = None) -> None:
    try:
        supabase.table(AUTOMATION_EVENT_TABLE).insert(
            {"event_type": event_type, "reference_table": reference_table, "reference_id": reference_id}
        ).execute()
    except Exception:
        pass


def _upsert_insight(dedupe_key: str, condition: bool, payload_fn) -> None:
    try:
        existing_rows = (
            supabase.table(INSIGHT_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
        )
    except Exception:
        return
    existing = existing_rows[0] if existing_rows else None

    if not condition:
        return  # Insights are not auto-resolved — a past business change stays a fact, unlike a broken link.

    if existing is not None:
        if existing.get("status") in TERMINAL_INSIGHT_STATUSES:
            return
        payload = payload_fn()
        payload["updated_at"] = _now_iso()
        try:
            supabase.table(INSIGHT_TABLE).update(payload).eq("dedupe_key", dedupe_key).execute()
        except Exception:
            pass
        return

    payload = payload_fn()
    payload.update({"dedupe_key": dedupe_key, "status": "erkannt", "source": "regelbasiert"})
    try:
        supabase.table(INSIGHT_TABLE).insert(payload).execute()
    except Exception:
        return
    _log_automation_event("insight_erkannt", reference_table=INSIGHT_TABLE, reference_id=dedupe_key)


def _pct_change(current: int, previous: int) -> float | None:
    if previous == 0:
        return None  # Division by zero — no meaningful percentage, no insight.
    return round((current - previous) / previous * 100)


def _detect_user_growth() -> None:
    this_week, previous_week = metrics.get_weekly_new_users()
    if this_week is None or previous_week is None:
        return
    guarded_this_week, note = metrics.small_group_guard(this_week)
    if guarded_this_week is None:
        return
    change = _pct_change(this_week, previous_week)
    if change is None or change < SIGNIFICANT_CHANGE_PCT:
        return

    today = date.today()
    week_ago = today - timedelta(days=7)
    two_weeks_ago = today - timedelta(days=14)

    def _build():
        return {
            "title": f"Neue Nutzerregistrierungen sind um {change}% gestiegen",
            "category": "wachstumschance",
            "description": f"In den letzten 7 Tagen haben sich {this_week} neue Nutzer registriert, in den 7 Tagen davor {previous_week}.",
            "data_basis": "vt_users.created_at, Zeitfenster-Vergleich (7 vs. 7 Tage).",
            "period_start": week_ago.isoformat(),
            "period_end": today.isoformat(),
            "comparison_period_start": two_weeks_ago.isoformat(),
            "comparison_period_end": week_ago.isoformat(),
            "severity": "mittel",
            "confidence": "mittel" if this_week < 20 else "hoch",
            "possible_cause": "Marketing-Aktivität, Saisonalität oder organisches Wachstum — ohne Kanal-Tracking nicht weiter zuordenbar.",
            "possible_impact": "Mehr potenzielle Premium-Konvertierungen, höhere Support-/Infrastrukturlast.",
            "recommended_action": "Prüfen, welcher Kanal die neuen Registrierungen erklärt, und ggf. verstärken.",
            "estimated_effort": "gering",
            "expected_benefit": "Wachstumstreiber gezielt verstärken statt zufällig zu warten.",
            "source_references": {"table": "vt_users", "this_week": this_week, "previous_week": previous_week},
        }

    _upsert_insight(f"growth_users_{week_ago.isoformat()}", True, _build)


def _detect_affiliate_category_change() -> None:
    this_week = metrics.get_affiliate_revenue_by_category(days=7)
    last_week_incl = metrics.get_affiliate_revenue_by_category(days=14)
    if not this_week and not last_week_incl:
        return

    today = date.today()
    week_ago = today - timedelta(days=7)
    two_weeks_ago = today - timedelta(days=14)

    for category, revenue_this_week in this_week.items():
        revenue_two_weeks = last_week_incl.get(category, 0.0)
        revenue_previous_week = max(revenue_two_weeks - revenue_this_week, 0.0)
        if revenue_previous_week <= 0:
            continue  # No meaningful percentage without a real previous baseline.
        change = round((revenue_this_week - revenue_previous_week) / revenue_previous_week * 100)
        if change < SIGNIFICANT_CHANGE_PCT:
            continue

        def _build(category=category, revenue_this_week=revenue_this_week, revenue_previous_week=revenue_previous_week, change=change):
            return {
                "title": f"Affiliate-Umsatz der Kategorie {category} ist um {change}% gestiegen",
                "category": "affiliate_chance",
                "description": f"{revenue_this_week:.2f} € diese Woche gegenüber {revenue_previous_week:.2f} € letzte Woche.",
                "data_basis": "vt_affiliate_events (event_type='conversion') je Produktkategorie, Zeitfenster-Vergleich (7 vs. 7 Tage).",
                "period_start": week_ago.isoformat(),
                "period_end": today.isoformat(),
                "comparison_period_start": two_weeks_ago.isoformat(),
                "comparison_period_end": week_ago.isoformat(),
                "severity": "mittel",
                "confidence": "mittel",
                "possible_cause": "Saisonalität, neue freigegebene Produkte in dieser Kategorie oder gestiegene Klicks.",
                "possible_impact": "Zusätzliches Wachstumspotenzial, falls die Kategorie priorisiert wird.",
                "recommended_action": "Kategorie im Affiliate Center priorisieren, ggf. weitere Produkte dieser Kategorie freigeben.",
                "estimated_effort": "gering",
                "expected_benefit": "Fortführung des positiven Trends durch gezielte Priorisierung.",
                "source_references": {"table": "vt_affiliate_events", "category": category},
            }

        _upsert_insight(f"affiliate_category_{category}_{week_ago.isoformat()}", True, _build)


def _detect_support_volume_change() -> None:
    this_week, previous_week = metrics.get_weekly_feedback_counts()
    if this_week is None or previous_week is None:
        return
    guarded_this_week, _note = metrics.small_group_guard(this_week)
    if guarded_this_week is None:
        return
    change = _pct_change(this_week, previous_week)
    if change is None or change < SIGNIFICANT_CHANGE_PCT:
        return

    today = date.today()
    week_ago = today - timedelta(days=7)
    two_weeks_ago = today - timedelta(days=14)

    def _build():
        return {
            "title": f"Support-Feedback-Volumen ist um {change}% gestiegen",
            "category": "supportproblem",
            "description": f"{this_week} Rückmeldungen in den letzten 7 Tagen gegenüber {previous_week} in den 7 Tagen davor.",
            "data_basis": "vt_user_feedback.created_at, Zeitfenster-Vergleich (7 vs. 7 Tage).",
            "period_start": week_ago.isoformat(),
            "period_end": today.isoformat(),
            "comparison_period_start": two_weeks_ago.isoformat(),
            "comparison_period_end": week_ago.isoformat(),
            "severity": "hoch" if change >= 50 else "mittel",
            "confidence": "mittel",
            "possible_cause": "Ein neues Problem, ein Release mit Nebenwirkungen, oder mehr aktive Nutzer insgesamt.",
            "possible_impact": "Unzufriedene Nutzer, höhere Abwanderungsgefahr, falls unbeantwortet.",
            "recommended_action": "Feedback-Einträge der letzten 7 Tage sichten und nach wiederkehrenden Themen gruppieren.",
            "estimated_effort": "mittel",
            "expected_benefit": "Frühes Erkennen eines systemischen Problems, bevor es sich auf mehr Nutzer ausweitet.",
            "source_references": {"table": "vt_user_feedback", "this_week": this_week, "previous_week": previous_week},
        }

    _upsert_insight(f"support_volume_{week_ago.isoformat()}", True, _build)


def run_insight_detection() -> None:
    """Runs every implemented insight rule once. Called synchronously from
    `GET /api/admin/founder/business-coach/dashboard` — no scheduler, no
    background job, matching every other Founder-OS module."""
    _detect_user_growth()
    _detect_affiliate_category_change()
    _detect_support_volume_change()


def send_insight_to_task_manager(insight: dict, *, admin_email: str) -> dict | None:
    """Hands an insight off as an operational, low-risk follow-up task in
    the AI Founder Task Manager (Submodule C) — reuses the existing
    `vt_founder_tasks` table directly rather than building a parallel task
    system. Deduplicated via a stable dedupe_key referencing the insight,
    so re-sending the same insight never creates a second task."""
    dedupe_key = f"business_coach_insight_{insight['id']}"
    try:
        existing = supabase.table(TASK_TABLE).select("id").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    except Exception:
        return None
    if existing:
        return None  # Already handed off — no duplicate.

    payload = {
        "dedupe_key": dedupe_key,
        "title": f"Business-Insight prüfen: {insight['title']}",
        "category": "business",
        "priority": "hoch" if insight.get("severity") in ("kritisch", "hoch") else "mittel",
        "status": "neu",
        "reason": f"Vom AI Business Coach übergebenes Insight (Kategorie: {insight.get('category')}).",
        "data_used": insight.get("data_basis", ""),
        "impact_if_ignored": insight.get("possible_impact") or "Nicht spezifiziert.",
        "suggested_action": None,
        "suggested_action_available": False,
        "auto_detected": True,
        "auto_resolved": False,
        "ignored": False,
    }
    try:
        result = supabase.table(TASK_TABLE).insert(payload).execute()
    except Exception:
        return None
    _log_automation_event("aufgabe_erstellt", reference_table=TASK_TABLE, reference_id=dedupe_key)
    try:
        supabase.table(INSIGHT_TABLE).update(
            {"status": "als_aufgabe_erstellt", "updated_at": _now_iso()}
        ).eq("id", insight["id"]).execute()
    except Exception:
        pass
    return result.data[0] if result.data else payload


def send_recommendation_to_approval_center(recommendation: dict, *, admin_email: str) -> dict | None:
    """Hands a recommendation with real business impact off to the Smart
    Approval Center (Submodule D) — reuses `vt_founder_approvals` directly.
    Never auto-approved; the founder must still decide there. Deduplicated
    via a stable dedupe_key referencing the recommendation."""
    dedupe_key = f"business_coach_recommendation_{recommendation['id']}"
    try:
        existing = supabase.table(APPROVAL_TABLE).select("id").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    except Exception:
        return None
    if existing:
        return None

    payload = {
        "dedupe_key": dedupe_key,
        "title": f"Business-Empfehlung: {recommendation['title']}",
        "category": "business",
        "source": "business_coach",
        "priority": recommendation.get("priority", "mittel"),
        "status": "ki_geprueft",
        "reason": recommendation.get("reasoning", ""),
        "data_used": recommendation.get("data_basis", ""),
        "rules_applied": "Regelbasierte Business-Coach-Empfehlung, keine automatische Ausführung.",
        "benefits": recommendation.get("expected_benefit") or "Nicht spezifiziert.",
        "risks": recommendation.get("risk") or "Nicht spezifiziert.",
        "related_entity_type": None,
        "related_entity_id": None,
        "auto_detected": True,
    }
    try:
        result = supabase.table(APPROVAL_TABLE).insert(payload).execute()
    except Exception:
        return None
    _log_automation_event("freigabe_vorbereitet", reference_table=APPROVAL_TABLE, reference_id=dedupe_key)
    try:
        supabase.table("vt_founder_business_recommendations").update(
            {"status": "an_approval_center_uebergeben", "updated_at": _now_iso()}
        ).eq("id", recommendation["id"]).execute()
    except Exception:
        pass
    return result.data[0] if result.data else payload
