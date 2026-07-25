"""Founder Dashboard — VitalTwin Release F1 (Founder Operating System,
Module 1).

Mounted at `/api/admin/founder` in `app/main.py`. One read-only endpoint,
`GET /dashboard`, aggregating real KPIs already computed elsewhere in the
Admin Control Center (Nutzer, Umsatz, KI, Affiliate, System, Aufgaben) into
a single response for the consolidated Founder view.

**Explicitly out of scope for this module** (per spec — "NICHT BAUEN"):
no automation, no AI-generated summaries, no scheduled reports, no
background jobs. This file only reads data that already exists; it never
writes, triggers, or schedules anything.

**No placeholders, no demo data, no fabricated numbers.** Every field is
either a real number computed from an actual table right now, or an
explicit `None` with a `*_note` explaining what's missing (e.g. "kein
Stripe-Reporting implementiert") — mirroring the same honesty pattern
already used in `routers/admin.py` (`business_overview`, `ai_usage`,
`system/status`). A premium-revenue estimate (users × list price) is
deliberately NOT computed here, since it would not be a real billed amount
(discounts/refunds/partial months would make it fictional).
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

from fastapi import APIRouter, Header

from ..core.admin_rbac import require_admin_permission
from ..core.supabase import supabase

router = APIRouter()

USER_TABLE = "vt_users"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
CHAT_USAGE_TABLE = "vt_chat_usage"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
AFFILIATE_EVENT_TABLE = "vt_affiliate_events"


def _count(table: str, *, filters: dict[str, object] | None = None) -> int | None:
    """Best-effort exact row count — `None` (not `0`) on failure, so the
    frontend can distinguish "genuinely zero" from "couldn't be determined"."""
    try:
        query = supabase.table(table).select("*", count="exact")
        for field, value in (filters or {}).items():
            query = query.eq(field, value)
        return query.execute().count
    except Exception:
        return None


def _new_users_7d(week_ago: str) -> int | None:
    try:
        return supabase.table(USER_TABLE).select("email", count="exact").gte("created_at", week_ago).execute().count
    except Exception:
        return None


def _active_users_7d(week_ago: str) -> int | None:
    try:
        active_rows = supabase.table(DAILY_ENTRY_TABLE).select("email").gte("entry_date", week_ago).execute().data or []
        return len({row["email"] for row in active_rows if row.get("email")})
    except Exception:
        return None


def _affiliate_revenue_total() -> float | None:
    try:
        conversions = (
            supabase.table(AFFILIATE_EVENT_TABLE).select("revenue").eq("event_type", "conversion").execute().data or []
        )
        return sum(float(row.get("revenue") or 0) for row in conversions)
    except Exception:
        return None


def _ai_requests_total() -> int | None:
    try:
        usage_rows = supabase.table(CHAT_USAGE_TABLE).select("count").execute().data or []
        return sum(int(row.get("count", 0)) for row in usage_rows)
    except Exception:
        return None


@router.get("/dashboard")
async def founder_dashboard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")

    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()

    # All 9 lookups below are fully independent (different tables/filters) —
    # run them concurrently in worker threads instead of one-by-one, so the
    # dashboard's total load time is roughly the slowest single Supabase
    # round-trip instead of the sum of all of them. This also keeps the
    # single-process event loop free to serve other requests while these
    # blocking supabase-py calls are in flight.
    (
        total_users,
        new_users_7d,
        active_users_7d,
        premium_users,
        affiliate_revenue,
        ai_requests_total,
        active_products,
        pending_approval,
        broken_links,
    ) = await asyncio.gather(
        asyncio.to_thread(_count, USER_TABLE),
        asyncio.to_thread(_new_users_7d, week_ago),
        asyncio.to_thread(_active_users_7d, week_ago),
        asyncio.to_thread(_count, USER_TABLE, filters={"premium": True}),
        asyncio.to_thread(_affiliate_revenue_total),
        asyncio.to_thread(_ai_requests_total),
        asyncio.to_thread(_count, AFFILIATE_PRODUCT_TABLE, filters={"status": "active"}),
        asyncio.to_thread(_count, AFFILIATE_PRODUCT_TABLE, filters={"status": "in_review"}),
        asyncio.to_thread(_count, AFFILIATE_PRODUCT_TABLE, filters={"link_status": "broken"}),
    )

    # --- 5. System -------------------------------------------------------------
    database_reachable = total_users is not None

    return {
        "users": {
            "total": total_users,
            "new_7d": new_users_7d,
            "active_7d": active_users_7d,
            "premium": premium_users,
        },
        "revenue": {
            "stripe": None,
            "stripe_note": "Kein Stripe-Reporting implementiert (erfordert Stripe-Reporting-API-Anbindung).",
            "affiliate": affiliate_revenue,
            "premium": None,
            "premium_note": (
                "Kein Umsatz-Reporting implementiert. Nutzerzahl mal Listenpreis wäre eine Schätzung, keine "
                "echte Zahl (Rabatte/Rückerstattungen/anteilige Monate nicht berücksichtigt) — daher hier "
                "bewusst nicht berechnet."
            ),
        },
        "ai": {
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "requests_total": ai_requests_total,
            "errors": None,
            "errors_note": "Kein Error-Tracking-System integriert.",
            "cost": None,
            "cost_note": "Kein Kosten-Tracking implementiert (erfordert OpenAI-Nutzungs-API-Anbindung).",
        },
        "affiliate": {
            "active_products": active_products,
            "broken_links": broken_links,
            "pending_approval": pending_approval,
        },
        "system": {
            "database": "reachable" if database_reachable else "unreachable",
            "api": "online",
            "server": None,
            "server_note": "Keine Server-Monitoring-Integration vorhanden.",
            "build_status": None,
            "build_status_note": "Keine CI/CD-Status-Integration vorhanden.",
        },
        "tasks": {
            "products_to_review": pending_approval,
            "broken_links": broken_links,
            "open_releases": None,
            "open_releases_note": "Keine Release-Tracking-Integration vorhanden.",
            "open_bugs": None,
            "open_bugs_note": "Kein Bug-Tracking-System integriert.",
        },
    }
