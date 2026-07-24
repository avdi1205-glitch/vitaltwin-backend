"""Admin Control Center — VitalTwin Enterprise Release 1.0.

Endpoints (mounted at `/api/admin` in `app/main.py`). Every endpoint calls
`core/admin_rbac.py::require_admin_permission` first — no admin endpoint in
this file skips the permission check, no matter how trivial the data looks.

Sections (matching the spec 1:1):

- `/dashboard`                     Admin Dashboard
- `/users*`                        User Management
- `/security/*`                    Security Center
- `/system/status`                 System Center
- `/support/feedback`              Support Center
- `/analytics/growth`              Analytics
- `/content*`                      Content Management
- `/ai/usage`                      KI Control Center
- `/business/overview`             Business Center
- `/nutrition/overview`            Nutrition & CGM (honest stub — see below)

Every "not implemented" area (revenue reporting, token/cost tracking,
affiliate programs, coupons, Health Connect/Apple Health, cron/queues) says
so explicitly in its response payload instead of fabricating numbers —
see `docs/ADMIN_ARCHITECTURE.md` for the full rationale per section.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.admin_rbac import ROLE_PERMISSIONS, require_admin, require_admin_permission
from ..core.audit import record_audit_event
from ..core.plans import get_configured_price_id
from ..core.supabase import supabase
from ..services.privacy_export import resolve_current_consents
from .users import set_premium_by_email

router = APIRouter()

USER_TABLE = "vt_users"
ADMIN_ROLE_TABLE = "vt_admin_roles"
LOGIN_EVENT_TABLE = "vt_login_events"
CONTENT_TABLE = "vt_content_items"
FEEDBACK_TABLE = "vt_user_feedback"
CONSENT_TABLE = "vt_consent_records"
AUDIT_TABLE = "vt_audit_events"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
CHAT_USAGE_TABLE = "vt_chat_usage"

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_LIST_LIMIT = 200

ALLOWED_CONTENT_TYPES = {"blog", "faq", "landing_page", "help_page", "notification"}
ALLOWED_CONTENT_STATUSES = {"draft", "published", "archived"}


def _paginate(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    start = (page - 1) * page_size
    end = start + page_size - 1
    return start, end


def _count_rows(table: str, *, filters: dict[str, object] | None = None) -> int | None:
    """Best-effort exact row count. Returns `None` (not `0`) on failure so
    callers can distinguish "genuinely zero" from "couldn't be determined"
    — never silently report a fabricated zero."""
    try:
        query = supabase.table(table).select("*", count="exact")
        for field, value in (filters or {}).items():
            query = query.eq(field, value)
        response = query.execute()
        return response.count
    except Exception:
        return None


class SuspendInput(BaseModel):
    reason: str | None = None


class RoleInput(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        if value not in ROLE_PERMISSIONS:
            raise ValueError(f"Ungültige Rolle. Erlaubt: {', '.join(sorted(ROLE_PERMISSIONS))}")
        return value


class PremiumInput(BaseModel):
    premium: bool


class ContentInput(BaseModel):
    content_type: str
    title: str
    slug: str | None = None
    body: str | None = None
    status: str = "draft"

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, value: str) -> str:
        if value not in ALLOWED_CONTENT_TYPES:
            raise ValueError(f"Ungültiger Content-Typ. Erlaubt: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_CONTENT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_CONTENT_STATUSES))}")
        return value


# ---------------------------------------------------------------------------
# Admin Dashboard
# ---------------------------------------------------------------------------


@router.get("/me")
async def get_current_admin(authorization: str | None = Header(default=None)):
    """Lets the frontend discover the caller's own admin role and permission
    set once (e.g. right after login) to build an RBAC-aware navigation,
    instead of probing every endpoint with trial requests."""
    principal = require_admin(authorization)
    return {
        "email": principal.email,
        "role": principal.role,
        "permissions": sorted(ROLE_PERMISSIONS.get(principal.role, frozenset())),
    }


@router.get("/dashboard")
async def admin_dashboard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_dashboard")

    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()

    total_users = _count_rows(USER_TABLE)
    premium_users = _count_rows(USER_TABLE, filters={"premium": True})
    suspended_users = _count_rows(USER_TABLE, filters={"suspended": True})

    try:
        registrations_7d = supabase.table(USER_TABLE).select("email", count="exact").gte("created_at", week_ago).execute().count
    except Exception:
        registrations_7d = None
    try:
        registrations_30d = supabase.table(USER_TABLE).select("email", count="exact").gte("created_at", month_ago).execute().count
    except Exception:
        registrations_30d = None

    try:
        active_rows = supabase.table(DAILY_ENTRY_TABLE).select("email").gte("entry_date", week_ago).execute().data or []
        active_users_7d: int | None = len({row["email"] for row in active_rows if row.get("email")})
    except Exception:
        active_users_7d = None

    open_feedback_count = _count_rows(FEEDBACK_TABLE)

    try:
        usage_rows = supabase.table(CHAT_USAGE_TABLE).select("count").eq("usage_date", today.isoformat()).execute().data or []
        ai_requests_today: int | None = sum(int(row.get("count", 0)) for row in usage_rows)
    except Exception:
        ai_requests_today = None

    return {
        "user_count": total_users,
        "premium_users": premium_users,
        "suspended_users": suspended_users,
        "registrations_7d": registrations_7d,
        "registrations_30d": registrations_30d,
        "active_users_7d": active_users_7d,
        "ai_requests_today": ai_requests_today,
        "open_feedback_count": open_feedback_count,
        "stripe_configured": bool(os.getenv("STRIPE_SECRET_KEY", "").strip()),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "supabase_reachable": total_users is not None,
        "revenue_note": "Umsatzzahlen erfordern eine Stripe-Reporting-API-Anbindung — nicht implementiert.",
        "error_tracking_note": "Kein Error-Tracking-System (z. B. Sentry) integriert — Fehleranzahl nicht verfügbar.",
        "system_messages": [],
    }


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    search: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, authorization: str | None = Header(default=None)
):
    require_admin_permission(authorization, "view_users")
    start, end = _paginate(page, page_size)

    try:
        # Never select `password` — Etappe "Keine Passwörter anzeigen".
        query = supabase.table(USER_TABLE).select("email,full_name,premium,suspended,created_at", count="exact")
        if search.strip():
            escaped = search.strip().replace("%", "")
            query = query.or_(f"email.ilike.%{escaped}%,full_name.ilike.%{escaped}%")
        response = query.order("created_at", desc=True).range(start, end).execute()
        users = response.data or []
        total = response.count or 0
    except Exception:
        users = []
        total = 0

    return {"items": users, "page": page, "page_size": page_size, "total": total}


@router.get("/users/{email}")
async def get_user_detail(email: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_users")
    email = email.strip().lower()

    try:
        rows = (
            supabase.table(USER_TABLE)
            .select("email,full_name,premium,suspended,suspended_reason,created_at,updated_at")
            .eq("email", email)
            .limit(1)
            .execute()
            .data
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzer konnte nicht geladen werden.") from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    try:
        consent_rows = supabase.table(CONSENT_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        consent_rows = []

    try:
        role_rows = supabase.table(ADMIN_ROLE_TABLE).select("role").eq("email", email).limit(1).execute().data or []
    except Exception:
        role_rows = []

    try:
        login_history = (
            supabase.table(LOGIN_EVENT_TABLE)
            .select("success,ip_address,created_at")
            .eq("email", email)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
    except Exception:
        login_history = []

    return {
        "user": rows[0],
        "consents": resolve_current_consents(consent_rows),
        "admin_role": role_rows[0]["role"] if role_rows else None,
        "recent_logins": login_history,
    }


@router.post("/users/{email}/suspend")
async def suspend_user(email: str, data: SuspendInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_users")
    email = email.strip().lower()
    try:
        supabase.table(USER_TABLE).update(
            {
                "suspended": True,
                "suspended_at": datetime.now(timezone.utc).isoformat(),
                "suspended_reason": data.reason,
            }
        ).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzer konnte nicht gesperrt werden.") from exc

    record_audit_event(
        user_id=None,
        email=admin.email,
        action="update",
        entity_type="user_suspension",
        entity_id=email,
        metadata={"suspended": True, "reason": data.reason},
    )
    return {"message": "Nutzer gesperrt.", "email": email}


@router.post("/users/{email}/unsuspend")
async def unsuspend_user(email: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_users")
    email = email.strip().lower()
    try:
        supabase.table(USER_TABLE).update({"suspended": False, "suspended_reason": None}).eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzer konnte nicht entsperrt werden.") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="user_suspension", entity_id=email,
        metadata={"suspended": False},
    )
    return {"message": "Nutzer entsperrt.", "email": email}


@router.post("/users/{email}/role")
async def set_user_role(email: str, data: RoleInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_roles")
    email = email.strip().lower()
    now = datetime.now(timezone.utc).isoformat()

    try:
        existing = supabase.table(ADMIN_ROLE_TABLE).select("id").eq("email", email).limit(1).execute().data
        if existing:
            supabase.table(ADMIN_ROLE_TABLE).update({"role": data.role, "updated_at": now}).eq("email", email).execute()
        else:
            supabase.table(ADMIN_ROLE_TABLE).insert(
                {"email": email, "role": data.role, "granted_by": admin.email}
            ).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Rolle konnte nicht gesetzt werden.") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="admin_role", entity_id=email,
        metadata={"role": data.role},
    )
    return {"message": "Rolle aktualisiert.", "email": email, "role": data.role}


@router.delete("/users/{email}/role")
async def remove_user_role(email: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_roles")
    email = email.strip().lower()
    try:
        supabase.table(ADMIN_ROLE_TABLE).delete().eq("email", email).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Rolle konnte nicht entfernt werden.") from exc

    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="admin_role", entity_id=email)
    return {"message": "Admin-Rolle entfernt.", "email": email}


@router.post("/users/{email}/premium")
async def set_user_premium(email: str, data: PremiumInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_premium")
    email = email.strip().lower()
    updated = set_premium_by_email(email, data.premium)
    if not updated:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="user_premium", entity_id=email,
        metadata={"premium": data.premium},
    )
    return {"message": "Premium-Status aktualisiert.", "email": email, "premium": data.premium}


@router.get("/users/{email}/login-history")
async def get_user_login_history(email: str, limit: int = 20, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_login_history")
    email = email.strip().lower()
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    try:
        rows = (
            supabase.table(LOGIN_EVENT_TABLE)
            .select("success,ip_address,user_agent,created_at")
            .eq("email", email)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    return {"items": rows}


# ---------------------------------------------------------------------------
# Security Center
# ---------------------------------------------------------------------------


@router.get("/security/audit-logs")
async def get_audit_logs(limit: int = 50, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_security")
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    try:
        rows = supabase.table(AUDIT_TABLE).select("*").order("created_at", desc=True).limit(limit).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.get("/security/login-history")
async def get_global_login_history(limit: int = 50, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_security")
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    try:
        rows = supabase.table(LOGIN_EVENT_TABLE).select("*").order("created_at", desc=True).limit(limit).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.get("/security/permissions")
async def get_permission_matrix(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_security")
    return {"roles": {role: sorted(permissions) for role, permissions in ROLE_PERMISSIONS.items()}}


# ---------------------------------------------------------------------------
# System Center
# ---------------------------------------------------------------------------


@router.get("/system/status")
async def system_status(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_system_status")
    db_reachable = _count_rows(USER_TABLE) is not None
    return {
        "database": {"status": "reachable" if db_reachable else "unreachable"},
        "openai": {"configured": bool(os.getenv("OPENAI_API_KEY", "").strip())},
        "stripe": {"configured": bool(os.getenv("STRIPE_SECRET_KEY", "").strip())},
        "storage": {"note": "Kein separates Objekt-Storage in Nutzung — keine Statusprüfung nötig."},
        "cron_jobs": {"note": "Keine Cron-Jobs/Background-Worker im aktuellen System implementiert."},
        "queues": {"note": "Keine Message-Queue im aktuellen System implementiert."},
        "health_connect": {"note": "Keine Health-Connect-Anbindung vorhanden."},
        "apple_health": {"note": "Keine Apple-Health-Anbindung vorhanden."},
    }


# ---------------------------------------------------------------------------
# Support Center
# ---------------------------------------------------------------------------


@router.get("/support/feedback")
async def list_feedback(
    page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, authorization: str | None = Header(default=None)
):
    require_admin_permission(authorization, "view_support")
    start, end = _paginate(page, page_size)
    try:
        response = (
            supabase.table(FEEDBACK_TABLE)
            .select("*", count="exact")
            .order("created_at", desc=True)
            .range(start, end)
            .execute()
        )
        items = response.data or []
        total = response.count or 0
    except Exception:
        items = []
        total = 0
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "note": (
            "Feedback, Bug Reports und Feature Requests laufen aktuell über ein gemeinsames Formular "
            "(`vt_user_feedback`) ohne separate Kategorisierung."
        ),
    }


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get("/analytics/growth")
async def analytics_growth(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_analytics")
    today = date.today()

    try:
        all_users = supabase.table(USER_TABLE).select("created_at,premium").execute().data or []
    except Exception:
        all_users = []

    registrations_by_day: dict[str, int] = {}
    premium_count = 0
    for user in all_users:
        if user.get("premium"):
            premium_count += 1
        created = user.get("created_at")
        if created:
            day = str(created)[:10]
            registrations_by_day[day] = registrations_by_day.get(day, 0) + 1

    try:
        checkin_rows = supabase.table(DAILY_ENTRY_TABLE).select("email,entry_date").execute().data or []
    except Exception:
        checkin_rows = []

    dau_today = len({row["email"] for row in checkin_rows if row.get("entry_date") == today.isoformat()})
    month_start = (today - timedelta(days=30)).isoformat()
    mau_30d = len({row["email"] for row in checkin_rows if str(row.get("entry_date", "")) >= month_start})

    total_users = len(all_users)
    conversion_rate = round(premium_count / total_users, 3) if total_users else None

    return {
        "total_users": total_users,
        "premium_users": premium_count,
        "premium_conversion_rate": conversion_rate,
        "registrations_by_day": registrations_by_day,
        "dau_today": dau_today,
        "mau_30d": mau_30d,
        "retention_note": "Kohorten-Retention erfordert ein dediziertes Event-Tracking-System — nicht implementiert.",
        "session_duration_note": "Keine Session-Dauer-Messung implementiert (kein Frontend-Analytics-Tracking).",
        "feature_usage_note": "Feature-Nutzung im Detail nicht aggregiert — Rohdaten liegen in den jeweiligen Fachtabellen vor.",
    }


# ---------------------------------------------------------------------------
# Content Management
# ---------------------------------------------------------------------------


@router.get("/content")
async def list_content(content_type: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_content")
    try:
        query = supabase.table(CONTENT_TABLE).select("*")
        if content_type:
            query = query.eq("content_type", content_type)
        rows = query.order("updated_at", desc=True).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.post("/content")
async def create_content(data: ContentInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_content")
    payload = data.model_dump()
    payload["created_by"] = admin.email
    if data.status == "published":
        payload["published_at"] = datetime.now(timezone.utc).isoformat()

    try:
        response = supabase.table(CONTENT_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inhalt konnte nicht gespeichert werden.") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="create", entity_type="content_item",
        metadata={"content_type": data.content_type},
    )
    return response.data[0] if response.data else payload


@router.patch("/content/{content_id}")
async def update_content(content_id: str, data: ContentInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_content")
    payload = data.model_dump()
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if data.status == "published":
        payload["published_at"] = datetime.now(timezone.utc).isoformat()

    try:
        supabase.table(CONTENT_TABLE).update(payload).eq("id", content_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inhalt konnte nicht aktualisiert werden.") from exc

    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="content_item", entity_id=content_id)
    return {"message": "Inhalt aktualisiert."}


@router.delete("/content/{content_id}")
async def delete_content(content_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_content")
    try:
        supabase.table(CONTENT_TABLE).delete().eq("id", content_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inhalt konnte nicht gelöscht werden.") from exc

    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="content_item", entity_id=content_id)
    return {"message": "Inhalt gelöscht."}


# ---------------------------------------------------------------------------
# KI Control Center
# ---------------------------------------------------------------------------


@router.get("/ai/usage")
async def ai_usage(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_ai_usage")
    today = date.today()

    try:
        rows = supabase.table(CHAT_USAGE_TABLE).select("*").execute().data or []
    except Exception:
        rows = []

    total_requests = sum(int(row.get("count", 0)) for row in rows)
    unique_users = len({row["email"] for row in rows if row.get("email")})
    requests_today = sum(int(row.get("count", 0)) for row in rows if row.get("usage_date") == today.isoformat())

    return {
        "model_configured": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "total_requests_all_time": total_requests,
        "unique_users_all_time": unique_users,
        "requests_today": requests_today,
        "token_usage_note": "Kein Token-/Kosten-Tracking pro Anfrage implementiert (erfordert OpenAI-Nutzungs-API-Anbindung).",
        "response_time_note": "Keine Antwortzeit-Messung implementiert.",
        "prompt_versions_note": "Kein Prompt-Versionierungssystem — der Systemprompt ist aktuell fest im Code (`services/twin_conversation.py`).",
    }


# ---------------------------------------------------------------------------
# Business Center
# ---------------------------------------------------------------------------


@router.get("/business/overview")
async def business_overview(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_business")
    premium_users = _count_rows(USER_TABLE, filters={"premium": True})

    configured_prices = {
        "premium_monthly": get_configured_price_id("premium", "monthly") is not None,
        "premium_yearly": get_configured_price_id("premium", "yearly") is not None,
        "pro_monthly": get_configured_price_id("pro", "monthly") is not None,
        "pro_yearly": get_configured_price_id("pro", "yearly") is not None,
        "family_monthly": get_configured_price_id("family", "monthly") is not None,
        "family_yearly": get_configured_price_id("family", "yearly") is not None,
    }

    return {
        "premium_users": premium_users,
        "stripe_configured": bool(os.getenv("STRIPE_SECRET_KEY", "").strip()),
        "configured_plan_prices": configured_prices,
        "pro_family_note": "PRO/FAMILY sind in der Datenbank aktuell nicht von PREMIUM unterscheidbar (ein boolesches Flag).",
        "revenue_note": "Kein Umsatz-Reporting implementiert (erfordert Stripe-Reporting-API-Anbindung).",
        "affiliate_note": "Kein Affiliate-/Provisions-System implementiert.",
        "coupons_note": "Keine Gutschein-Verwaltung implementiert.",
    }


# ---------------------------------------------------------------------------
# Nutrition & CGM
# ---------------------------------------------------------------------------


@router.get("/nutrition/overview")
async def nutrition_overview(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_nutrition_admin")
    return {
        "available": False,
        "note": (
            "VitalTwin hat aktuell keine Nutrition-/CGM-Datenpipeline (kein Connector, kein Import, keine "
            "systemweite Datenqualitätsprüfung). Dieser Bereich ist strukturell vorbereitet (die Berechtigung "
            "`view_nutrition_admin` existiert bereits), zeigt aber ehrlich an, dass es noch nichts zu "
            "überwachen gibt, statt erfundene Kennzahlen darzustellen."
        ),
        "import_errors": [],
        "connector_status": [],
        "import_stats": {},
    }
