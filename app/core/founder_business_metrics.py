"""AI Business Coach — real business metrics aggregation (VitalTwin
Enterprise, Founder Operating System, Submodule E).

**No individual user data.** Every function here returns aggregated,
system-wide numbers only — never a per-user row, never Wellness/CGM/
Nutrition/Sleep/Movement/Twin-Memory data. This module reads from the
same tables the other Founder modules already read from (users, affiliate,
chat usage, feedback, content) — it does not duplicate their detection
logic, only aggregates raw counts for the coach's own KPI dashboard.

**No metric snapshot table.** Every number is computed fresh from the
already-timestamped raw tables on each call (same philosophy as
`routers/founder.py`/`routers/founder_briefing.py`) — trend comparisons
use two real time windows on the same raw data, never a cached/point-in-
time snapshot series that doesn't exist.

**Small-group privacy guard.** Any aggregate computed from fewer than
`MIN_GROUP_SIZE` underlying rows is suppressed (returned as `None` with a
note) — Etappe/Spec requirement "verhindere Rückschlüsse auf einzelne
Nutzer bei sehr kleinen Gruppen".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .ai_usage_logger import get_ai_usage_summary
from .concurrency import run_parallel
from .supabase import supabase
from . import stripe_billing

USER_TABLE = "vt_users"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
CHAT_USAGE_TABLE = "vt_chat_usage"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
AFFILIATE_EVENT_TABLE = "vt_affiliate_events"
FEEDBACK_TABLE = "vt_user_feedback"
CONTENT_TABLE = "vt_content_items"
TASK_TABLE = "vt_founder_tasks"
APPROVAL_TABLE = "vt_founder_approvals"

MIN_GROUP_SIZE = 5

NO_PREMIUM_TIMESTAMP_NOTE = (
    "Keine zeitgestempelten Premium-Aktivierungen gespeichert — der Stripe-Webhook setzt nur das "
    "boolesche premium-Flag ohne Datum, daher nicht rückwirkend zählbar."
)
NO_CANCELLATION_NOTE = "vt_stripe_subscriptions nicht erreichbar oder Migration 023 noch nicht ausgeführt."
NO_COST_NOTE = "vt_ai_usage_events nicht erreichbar oder Migration 022 noch nicht ausgeführt."
NO_INFRA_COST_NOTE = "Keine Infrastruktur-Kosten-Integration vorhanden (kein Railway-/Vercel-Billing-API-Zugriff)."
NO_MRR_NOTE = (
    "Kein wiederkehrender Monatsumsatz berechenbar — vt_users speichert nur ein boolesches premium-Flag, "
    "keinen Plan/Preis/Abrechnungszyklus pro Nutzer."
)


def small_group_guard(count: int | None) -> tuple[int | None, str | None]:
    """Returns `(value_or_None, note)` — suppresses aggregates computed
    from too few rows to protect individual users from re-identification."""
    if count is None:
        return None, "Datenquelle nicht erreichbar."
    if count < MIN_GROUP_SIZE:
        return None, f"Gruppengröße ({count}) zu klein für eine aussagekräftige, datenschutzfreundliche Anzeige."
    return count, None


def count_rows(table: str, *, filters: dict[str, object] | None = None, gte: tuple[str, str] | None = None) -> int | None:
    try:
        query = supabase.table(table).select("*", count="exact")
        for field, value in (filters or {}).items():
            query = query.eq(field, value)
        if gte:
            query = query.gte(*gte)
        return query.execute().count
    except Exception:
        return None


def get_business_dashboard() -> dict:
    """The compact KPI overview at the top of the Business Coach page."""
    now = datetime.now(timezone.utc)
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    today_start = today.isoformat()

    def _affiliate_revenue(since: str) -> float | None:
        try:
            events = (
                supabase.table(AFFILIATE_EVENT_TABLE)
                .select("revenue")
                .eq("event_type", "conversion")
                .gte("created_at", since)
                .execute()
                .data
                or []
            )
            return sum(float(r.get("revenue") or 0) for r in events)
        except Exception:
            return None

    def _open_tasks() -> int:
        try:
            return len(
                [
                    t
                    for t in (supabase.table(TASK_TABLE).select("status").execute().data or [])
                    if t.get("status") in ("neu", "in_bearbeitung", "warten")
                ]
            )
        except Exception:
            return 0

    def _open_approvals() -> int:
        try:
            return len(
                [
                    a
                    for a in (supabase.table(APPROVAL_TABLE).select("status").execute().data or [])
                    if a.get("status") in ("neu", "ki_geprueft", "zur_pruefung")
                ]
            )
        except Exception:
            return 0

    # All 9 lookups below are independent — run them concurrently instead
    # of one after another.
    (
        total_users,
        premium_users,
        affiliate_revenue_today,
        affiliate_revenue_month,
        open_tasks,
        open_approvals,
        revenue_summary,
        cancellations_month,
        ai_usage_today,
    ) = run_parallel(
        lambda: count_rows(USER_TABLE),
        lambda: count_rows(USER_TABLE, filters={"premium": True}),
        lambda: _affiliate_revenue(today_start),
        lambda: _affiliate_revenue(month_start),
        _open_tasks,
        _open_approvals,
        stripe_billing.get_revenue_summary,
        lambda: stripe_billing.get_cancellations_since(month_start),
        lambda: get_ai_usage_summary(days=1),
    )
    conversion_rate = round(premium_users / total_users, 3) if total_users and premium_users is not None and total_users > 0 else None

    return {
        "computed_at": now.isoformat(),
        "revenue_today": {
            "value": revenue_summary["revenue_today"],
            "note": None if revenue_summary["revenue_today"] is not None else revenue_summary["note"],
            "source": "vt_stripe_payments",
        },
        "revenue_month": {
            "value": revenue_summary["revenue_month"],
            "note": None if revenue_summary["revenue_month"] is not None else revenue_summary["note"],
            "source": "vt_stripe_payments",
        },
        "mrr": {"value": None, "note": NO_MRR_NOTE, "source": "vt_users (nicht ausreichend)"},
        "new_premium_subscriptions": {"value": None, "note": NO_PREMIUM_TIMESTAMP_NOTE, "source": "vt_users (nicht ausreichend)"},
        "cancellations": {
            "value": cancellations_month,
            "note": None if cancellations_month is not None else NO_CANCELLATION_NOTE,
            "source": "vt_stripe_subscriptions",
        },
        "affiliate_revenue_today": {"value": affiliate_revenue_today, "note": None, "source": "vt_affiliate_events"},
        "ai_cost": {
            "value": ai_usage_today.get("cost_usd"),
            "note": ai_usage_today.get("cost_note"),
            "source": "vt_ai_usage_events",
        },
        "infra_cost": {"value": None, "note": NO_INFRA_COST_NOTE, "source": "Railway/Vercel (nicht verbunden)"},
        "conversion_rate": {"value": conversion_rate, "note": None if conversion_rate is not None else "Keine Nutzer vorhanden.", "source": "vt_users"},
        "open_risks": {"value": None, "note": "Siehe Insights (Kategorie enthält 'risiko')", "source": "vt_founder_business_insights"},
        "open_opportunities": {"value": None, "note": "Siehe Insights (Kategorie enthält 'chance')", "source": "vt_founder_business_insights"},
        "open_founder_decisions": {"value": open_tasks + open_approvals, "note": None, "source": "vt_founder_tasks + vt_founder_approvals"},
    }


def get_weekly_new_users() -> tuple[int | None, int | None]:
    """Returns `(this_week, previous_week)` new registrations, or `None`
    on failure. Both windows come straight from `vt_users.created_at`."""
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    two_weeks_ago = (today - timedelta(days=14)).isoformat()

    this_week = count_rows(USER_TABLE, gte=("created_at", week_ago))
    total_two_weeks = count_rows(USER_TABLE, gte=("created_at", two_weeks_ago))
    if this_week is None or total_two_weeks is None:
        return None, None
    previous_week = max(total_two_weeks - this_week, 0)
    return this_week, previous_week


def get_weekly_feedback_counts() -> tuple[int | None, int | None]:
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    two_weeks_ago = (today - timedelta(days=14)).isoformat()

    this_week = count_rows(FEEDBACK_TABLE, gte=("created_at", week_ago))
    total_two_weeks = count_rows(FEEDBACK_TABLE, gte=("created_at", two_weeks_ago))
    if this_week is None or total_two_weeks is None:
        return None, None
    previous_week = max(total_two_weeks - this_week, 0)
    return this_week, previous_week


def get_affiliate_revenue_by_category(days: int = 7) -> dict[str, float]:
    """Real affiliate revenue per category name over the last `days` days —
    used both for the KPI dashboard and the "Affiliate-Kategorie X ist
    gestiegen" insight rule."""
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        events = (
            supabase.table(AFFILIATE_EVENT_TABLE)
            .select("product_id,revenue")
            .eq("event_type", "conversion")
            .gte("created_at", since)
            .execute()
            .data
            or []
        )
        products = supabase.table(AFFILIATE_PRODUCT_TABLE).select("id,category_id").execute().data or []
        categories = supabase.table("vt_affiliate_categories").select("id,name").execute().data or []
    except Exception:
        return {}

    category_name_by_id = {str(c["id"]): c["name"] for c in categories}
    category_by_product = {str(p["id"]): category_name_by_id.get(str(p.get("category_id")), "Unkategorisiert") for p in products}

    revenue_by_category: dict[str, float] = {}
    for event in events:
        category = category_by_product.get(str(event.get("product_id")), "Unkategorisiert")
        revenue_by_category[category] = revenue_by_category.get(category, 0.0) + float(event.get("revenue") or 0)
    return revenue_by_category


def get_published_content_count() -> int | None:
    return count_rows(CONTENT_TABLE, filters={"status": "published"})
