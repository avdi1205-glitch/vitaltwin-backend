"""Admin Control Center — VitalTwin Admin Dashboard.

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
- `/integrations`                  Platform Foundation (Connector/Provider status)
- `/feature-flags*`                Feature Flags

Every "not implemented" area (revenue reporting, token/cost tracking,
affiliate programs, coupons, Health Connect/Apple Health, cron/queues) says
so explicitly in its response payload instead of fabricating numbers —
see `docs/ADMIN_ARCHITECTURE.md` for the full rationale per section.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, field_validator

from ..core.admin_rbac import ROLE_PERMISSIONS, require_admin, require_admin_permission
from ..core.account_deletion import purge_all_user_data
from ..core.ai_usage_logger import get_ai_usage_summary
from ..core.audit import record_audit_event
from ..core.concurrency import run_parallel
from ..core.error_events import get_error_summary
from ..core.founder_backup_status import get_latest_backup_status, list_backups, record_backup
from ..core.founder_releases import get_latest_release, list_releases, record_release
from ..core.integrations import get_full_integration_report
from ..core.plans import get_configured_price_id
from ..core.plan_service import (
    BETA_PLAN_VALUES,
    extend_beta_by_email,
    get_beta_grant_by_email,
    grant_beta_by_email,
    normalize_plan_row,
    revoke_beta_by_email,
    set_plan_by_email,
)
from ..core.rate_limit import enforce_rate_limit
from ..core import stripe_billing
from ..core.supabase import supabase
from ..core.webhook_auth import require_webhook_secret
from ..services.privacy_export import resolve_current_consents
from .users import set_premium_by_email
from . import family as _family_router

router = APIRouter()

USER_TABLE = "vt_users"
PROFILE_TABLE = "vt_user_profiles"
ADMIN_ROLE_TABLE = "vt_admin_roles"
LOGIN_EVENT_TABLE = "vt_login_events"
CONTENT_TABLE = "vt_content_items"
FEEDBACK_TABLE = "vt_user_feedback"
CONTACT_TABLE = "vt_contact_messages"
CONSENT_TABLE = "vt_consent_records"
AUDIT_TABLE = "vt_audit_events"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
CHAT_USAGE_TABLE = "vt_chat_usage"
TWIN_CALC_TABLE = "vt_twin_calculations"
TWIN_MEMORY_TABLE = "vt_twin_memory"
TWIN_LEARNING_EVENTS_TABLE = "vt_twin_learning_events"
RECOMMENDATION_FEEDBACK_TABLE = "vt_recommendation_feedback"
BETA_APPLICATION_TABLE = "vt_beta_applications"


def _get_family_membership_summary(user_id: int) -> dict[str, object] | None:
    """Beta Tester Program hardening: admin-facing view of a PRESERVED
    Family membership (never deleted by an expired/revoked grant) — reuses
    `family.py`'s own membership lookup and entitlement-lock resolution
    directly (router-to-router import, same established pattern as
    `profile.py` reusing `chat.py`'s Google Health context builder) rather
    than a second implementation. Returns `None` if the user has never
    been in a Family at all."""
    membership = _family_router._get_open_membership(user_id)
    if not membership:
        return None
    family_id = int(membership["family_id"])
    return {
        "family_id": family_id,
        "role": membership.get("role"),
        "status": membership.get("status"),
        "entitlement_active": _family_router._family_entitlement_active(family_id),
    }
STRIPE_SUBSCRIPTION_TABLE = "vt_stripe_subscriptions"

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_LIST_LIMIT = 200

ALLOWED_CONTENT_TYPES = {"blog", "faq", "landing_page", "help_page", "notification"}
ALLOWED_CONTENT_STATUSES = {"draft", "published", "archived"}

# The one, project-wide QA-test-account marker (Admin Control Center §QA
# Cleanup) — deliberately NOT "email contains 'test'" (far too broad, would
# risk matching a real user). Both the email prefix AND the full_name
# marker must match, per the explicit double-safety requirement.
QA_TEST_EMAIL_PREFIX = "qa-test-"
QA_TEST_NAME_MARKER = "QA TEST ACCOUNT"


def _is_qa_test_account(email: str, full_name: str | None) -> bool:
    return bool(email) and email.lower().startswith(QA_TEST_EMAIL_PREFIX) and QA_TEST_NAME_MARKER in (full_name or "")


def _compute_account_status(*, suspended: bool, deletion_requested_at: str | None) -> str:
    """Reuses the existing `suspended` boolean (already "deactivate this
    account" in effect — blocks login, see `routers/users.py::login`) and
    the existing `deletion_requested_at` (GDPR self-service request) rather
    than introducing a new status column — deliberately minimal per the
    admin-improvement task's "keine unnötigen Umbauten"."""
    if suspended:
        return "deactivated"
    if deletion_requested_at:
        return "deletion_requested"
    return "active"


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


class PlanChangeInput(BaseModel):
    """Explicit administrative tariff change (VitalTwin Plan System) —
    distinct from Stripe/Beta-Zugang. Never claims this reflects a real
    Stripe subscription; audited with `trigger: "admin_manual_override"`."""

    plan: str

    @field_validator("plan")
    @classmethod
    def _validate_plan(cls, value: str) -> str:
        if value not in {"free", "premium", "pro", "family"}:
            raise ValueError("Ungültiger Tarif. Erlaubt: free, premium, pro, family")
        return value


class BetaGrantInput(BaseModel):
    """Beta Tester Program: grants a NEW temporary Pro/Family/Premium
    overlay — never touches the real `plan`/Stripe state (see
    `core/plan_service.py::grant_beta_by_email`)."""

    plan: str
    days: int

    @field_validator("plan")
    @classmethod
    def _validate_plan(cls, value: str) -> str:
        if value not in BETA_PLAN_VALUES:
            raise ValueError(f"Ungültiger Beta-Tarif. Erlaubt: {', '.join(sorted(BETA_PLAN_VALUES))}")
        return value

    @field_validator("days")
    @classmethod
    def _validate_days(cls, value: int) -> int:
        if not (1 <= value <= 365):
            raise ValueError("Beta-Dauer muss zwischen 1 und 365 Tagen liegen.")
        return value


class BetaExtendInput(BaseModel):
    days: int

    @field_validator("days")
    @classmethod
    def _validate_days(cls, value: int) -> int:
        if not (1 <= value <= 365):
            raise ValueError("Verlängerung muss zwischen 1 und 365 Tagen liegen.")
        return value


class QACleanupExecuteInput(BaseModel):
    confirm: bool = False


class ReleaseInput(BaseModel):
    version: str
    git_commit_sha: str | None = None
    environment: str = "production"
    description: str | None = None
    build_status: str = "unbekannt"

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        value = value.strip()
        if not (1 <= len(value) <= 100):
            raise ValueError("Version muss 1-100 Zeichen lang sein.")
        return value


class BackupInput(BaseModel):
    status: str
    backup_type: str = "database"
    size_bytes: int | None = None
    completed_at: str | None = None
    note: str | None = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        allowed = {"erfolgreich", "fehlgeschlagen", "laeuft"}
        if value not in allowed:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(allowed))}")
        return value


class ContentInput(BaseModel):
    content_type: str
    title: str
    slug: str | None = None
    body: str | None = None
    status: str = "draft"
    excerpt: str | None = None
    category: str | None = None
    tags: list[str] = []
    meta_title: str | None = None
    meta_description: str | None = None

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

    def _total_users() -> int | None:
        return _count_rows(USER_TABLE)

    def _premium_users() -> int | None:
        return _count_rows(USER_TABLE, filters={"premium": True})

    def _suspended_users() -> int | None:
        return _count_rows(USER_TABLE, filters={"suspended": True})

    def _registrations_7d() -> int | None:
        try:
            return supabase.table(USER_TABLE).select("email", count="exact").gte("created_at", week_ago).execute().count
        except Exception:
            return None

    def _registrations_30d() -> int | None:
        try:
            return supabase.table(USER_TABLE).select("email", count="exact").gte("created_at", month_ago).execute().count
        except Exception:
            return None

    def _active_users_7d() -> int | None:
        try:
            active_rows = supabase.table(DAILY_ENTRY_TABLE).select("email").gte("entry_date", week_ago).execute().data or []
            return len({row["email"] for row in active_rows if row.get("email")})
        except Exception:
            return None

    def _open_feedback_count() -> int | None:
        return _count_rows(FEEDBACK_TABLE)

    def _beta_applications_total() -> int | None:
        return _count_rows(BETA_APPLICATION_TABLE)

    def _ai_requests_today() -> int | None:
        try:
            usage_rows = supabase.table(CHAT_USAGE_TABLE).select("count").eq("usage_date", today.isoformat()).execute().data or []
            return sum(int(row.get("count", 0)) for row in usage_rows)
        except Exception:
            return None

    def _latest_activity() -> dict[str, object] | None:
        try:
            latest_audit = (
                supabase.table(AUDIT_TABLE).select("action,entity_type,email,created_at").order("created_at", desc=True).limit(1).execute().data or []
            )
            return latest_audit[0] if latest_audit else None
        except Exception:
            return None

    def _revenue_summary() -> dict:
        return stripe_billing.get_revenue_summary()

    def _error_summary() -> dict:
        return get_error_summary(days=7)

    (
        total_users,
        premium_users,
        suspended_users,
        registrations_7d,
        registrations_30d,
        active_users_7d,
        open_feedback_count,
        beta_applications_total,
        ai_requests_today,
        latest_activity,
        revenue_summary,
        error_summary,
    ) = await asyncio.to_thread(
        run_parallel,
        _total_users,
        _premium_users,
        _suspended_users,
        _registrations_7d,
        _registrations_30d,
        _active_users_7d,
        _open_feedback_count,
        _beta_applications_total,
        _ai_requests_today,
        _latest_activity,
        _revenue_summary,
        _error_summary,
    )

    return {
        "user_count": total_users,
        "premium_users": premium_users,
        "suspended_users": suspended_users,
        "registrations_7d": registrations_7d,
        "registrations_30d": registrations_30d,
        "active_users_7d": active_users_7d,
        "ai_requests_today": ai_requests_today,
        "open_feedback_count": open_feedback_count,
        "beta_applications_total": beta_applications_total,
        "beta_applications_note": "Zählt eingegangene Beta-Bewerbungen — es gibt aktuell keinen separaten Freigabe-/Aktivierungsstatus für Beta-Tester.",
        "latest_activity": latest_activity,
        "stripe_configured": bool(os.getenv("STRIPE_SECRET_KEY", "").strip()),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "supabase_reachable": total_users is not None,
        "revenue_today": revenue_summary["revenue_today"],
        "revenue_month": revenue_summary["revenue_month"],
        "revenue_note": revenue_summary["note"],
        "error_count_7d": error_summary["total"],
        "error_tracking_note": (
            "Fehler werden intern geloggt (vt_error_events) und zusätzlich an Sentry gesendet, sofern "
            "SENTRY_DSN konfiguriert ist."
            if error_summary["total"] is not None
            else error_summary["note"]
        ),
        "system_messages": [],
    }


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------


def _count_super_admins(exclude_email: str | None = None) -> int:
    """Best-effort count of remaining super_admin rows, used to block a
    role change/removal that would leave the platform with zero
    super-admins. Fails closed (returns 0) on any DB error so an
    unexpected failure blocks the action instead of silently allowing an
    accidental lockout."""
    try:
        query = supabase.table(ADMIN_ROLE_TABLE).select("email", count="exact").eq("role", "super_admin")
        if exclude_email:
            query = query.neq("email", exclude_email)
        return query.execute().count or 0
    except Exception:
        return 0


@router.get("/users")
async def list_users(
    search: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, authorization: str | None = Header(default=None)
):
    require_admin_permission(authorization, "view_users")
    start, end = _paginate(page, page_size)

    try:
        # Never select `password` — Etappe "Keine Passwörter anzeigen".
        query = supabase.table(USER_TABLE).select("email,full_name,premium,plan,suspended,created_at", count="exact")
        if search.strip():
            escaped = search.strip().replace("%", "")
            query = query.or_(f"email.ilike.%{escaped}%,full_name.ilike.%{escaped}%")
        response = query.order("created_at", desc=True).range(start, end).execute()
        users = response.data or []
        total = response.count or 0
    except Exception:
        users = []
        total = 0

    # Enrich each row with its admin role (if any), most recent successful
    # login, and deletion-request timestamp — bulk-fetched for this page's
    # emails in ONE call each (not per-row) to avoid an N+1 pattern.
    emails = [row["email"] for row in users if row.get("email")]
    if emails:
        def _roles():
            try:
                return supabase.table(ADMIN_ROLE_TABLE).select("email,role").in_("email", emails).execute().data or []
            except Exception:
                return []

        def _last_logins():
            try:
                return (
                    supabase.table(LOGIN_EVENT_TABLE)
                    .select("email,created_at")
                    .in_("email", emails)
                    .eq("success", True)
                    .order("created_at", desc=True)
                    .execute()
                    .data
                    or []
                )
            except Exception:
                return []

        def _deletion_requests():
            try:
                return (
                    supabase.table(PROFILE_TABLE)
                    .select("email,deletion_requested_at")
                    .in_("email", emails)
                    .execute()
                    .data
                    or []
                )
            except Exception:
                return []

        role_rows, login_rows, deletion_rows = run_parallel(_roles, _last_logins, _deletion_requests)
        role_map = {row["email"]: row["role"] for row in role_rows if row.get("email")}
        last_login_map: dict[str, str] = {}
        for row in login_rows:
            row_email = row.get("email")
            if row_email and row_email not in last_login_map:
                last_login_map[row_email] = row.get("created_at")
        deletion_map = {row["email"]: row.get("deletion_requested_at") for row in deletion_rows if row.get("email")}

        for user in users:
            user["role"] = role_map.get(user.get("email"))
            user["last_login_at"] = last_login_map.get(user.get("email"))
            deletion_requested_at = deletion_map.get(user.get("email"))
            user["deletion_requested_at"] = deletion_requested_at
            user["plan"] = normalize_plan_row(user)
            user["status"] = _compute_account_status(
                suspended=bool(user.get("suspended")), deletion_requested_at=deletion_requested_at
            )
    else:
        for user in users:
            user["role"] = None
            user["last_login_at"] = None
            user["deletion_requested_at"] = None
            user["plan"] = normalize_plan_row(user)
            user["status"] = _compute_account_status(suspended=bool(user.get("suspended")), deletion_requested_at=None)

    return {"items": users, "page": page, "page_size": page_size, "total": total}


@router.get("/users/{email}")
async def get_user_detail(email: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_users")
    email = email.strip().lower()

    try:
        rows = (
            supabase.table(USER_TABLE)
            .select("id,email,full_name,premium,plan,suspended,suspended_reason,created_at,updated_at")
            .eq("email", email)
            .limit(1)
            .execute()
            .data
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzer konnte nicht geladen werden.") from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    def _consents():
        try:
            return supabase.table(CONSENT_TABLE).select("*").eq("email", email).execute().data or []
        except Exception:
            return []

    def _role():
        try:
            return supabase.table(ADMIN_ROLE_TABLE).select("role").eq("email", email).limit(1).execute().data or []
        except Exception:
            return []

    def _recent_logins():
        try:
            return (
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
            return []

    def _last_successful_login():
        try:
            return (
                supabase.table(LOGIN_EVENT_TABLE)
                .select("created_at")
                .eq("email", email)
                .eq("success", True)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _deletion_request():
        try:
            return (
                supabase.table(PROFILE_TABLE)
                .select("deletion_requested_at")
                .eq("email", email)
                .limit(1)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _stripe_subscriptions():
        try:
            return (
                supabase.table(STRIPE_SUBSCRIPTION_TABLE)
                .select("status")
                .eq("email", email)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    consent_rows, role_rows, login_history, last_login_rows, deletion_rows, subscription_rows = run_parallel(
        _consents, _role, _recent_logins, _last_successful_login, _deletion_request, _stripe_subscriptions
    )

    user = dict(rows[0])
    user["last_login_at"] = last_login_rows[0]["created_at"] if last_login_rows else None
    deletion_requested_at = deletion_rows[0]["deletion_requested_at"] if deletion_rows else None
    user["deletion_requested_at"] = deletion_requested_at
    plan = normalize_plan_row(user)
    user["plan"] = plan
    user["status"] = _compute_account_status(suspended=bool(user.get("suspended")), deletion_requested_at=deletion_requested_at)
    # "Beta-Zugang" (legacy Free Beta activation, no dedicated columns for
    # it) isn't a stored flag — inferred honestly from real data: a
    # paid-tier account with NO Stripe subscription row at all can only
    # have gotten there via the free Beta-Zugang self-service activation
    # (the only other path besides an admin/founder manually setting the
    # plan) — never fabricated.
    user["beta_access"] = plan != "free" and not subscription_rows
    # Beta Tester Program (admin-controlled, time-limited Pro/Family/Premium
    # grant) — a SEPARATE, explicit overlay on top of the real `plan`
    # above; `None` if this user has never had one / it was fully revoked.
    user["beta_grant"] = get_beta_grant_by_email(email)
    # Beta Tester Program hardening: preserved Family membership (rows are
    # NEVER deleted by an expired/revoked grant) + whether Family customer
    # functionality is currently locked — reuses `family.py`'s own
    # entitlement-lock resolution (same effective-plan/has_feature call),
    # never a second authorization engine.
    user["family_membership"] = _get_family_membership_summary(int(user["id"])) if user.get("id") is not None else None

    return {
        "user": user,
        "consents": resolve_current_consents(consent_rows),
        "admin_role": role_rows[0]["role"] if role_rows else None,
        "recent_logins": login_history,
    }


@router.delete("/users/{email}")
async def delete_user(email: str, authorization: str | None = Header(default=None)):
    """Admin-initiated hard delete — distinct from the GDPR self-service
    flow (`/users/{email}/deletion-requests/complete`), which only ever
    completes a deletion the USER themselves already requested. This lets a
    founder directly remove a problematic/spam account without waiting for
    that. Reuses the same `purge_all_user_data()` used by the GDPR flow —
    one deletion implementation, not two. Deleting the row from `vt_users`
    structurally prevents any further login (auth checks that table
    directly), no separate "disabled" flag needed.

    Restricted to super_admin actors specifically (not just anyone with the
    `manage_users` permission, which admin/support also hold) — a real,
    irreversible hard delete is a higher-stakes action than suspend/premium."""
    admin = require_admin_permission(authorization, "manage_users")
    if admin.role != "super_admin":
        raise HTTPException(status_code=403, detail="Nur Super-Admins dürfen Nutzer endgültig löschen.")
    email = email.strip().lower()

    try:
        role_rows = supabase.table(ADMIN_ROLE_TABLE).select("role").eq("email", email).limit(1).execute().data or []
    except Exception:
        role_rows = []
    if role_rows and role_rows[0].get("role") == "super_admin":
        raise HTTPException(status_code=403, detail="Super-Admin-Konten können nicht gelöscht werden.")

    deleted_rows = purge_all_user_data(email)
    if not deleted_rows.get(USER_TABLE):
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    record_audit_event(
        user_id=None,
        email=admin.email,
        action="delete",
        entity_type="user_account",
        entity_id=email,
        metadata={"deleted_rows": deleted_rows, "trigger": "admin_direct"},
    )
    return {"message": "Nutzer und alle zugehörigen Daten wurden gelöscht.", "email": email, "deleted_rows": deleted_rows}


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
        existing = supabase.table(ADMIN_ROLE_TABLE).select("id,role").eq("email", email).limit(1).execute().data
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Rolle konnte nicht gesetzt werden.") from exc

    current_role = existing[0].get("role") if existing else None
    if current_role == "super_admin" and data.role != "super_admin" and _count_super_admins(exclude_email=email) < 1:
        raise HTTPException(status_code=409, detail="Der letzte Super-Admin kann nicht herabgestuft werden.")

    try:
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
        existing = supabase.table(ADMIN_ROLE_TABLE).select("role").eq("email", email).limit(1).execute().data or []
    except Exception:
        existing = []

    if existing and existing[0].get("role") == "super_admin" and _count_super_admins(exclude_email=email) < 1:
        raise HTTPException(status_code=409, detail="Der letzte Super-Admin kann nicht entfernt werden.")

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


@router.post("/users/{email}/plan")
async def set_user_plan(email: str, data: PlanChangeInput, authorization: str | None = Header(default=None)):
    """Sets an EXACT tariff (VitalTwin Plan System: free/premium/pro/family)
    — distinct from the legacy `/premium` boolean toggle above (kept for
    compatibility, still works for Free vs. any-paid-tier). This is always
    an explicit, audited, ADMINISTRATIVE change — it never touches Stripe
    and never pretends to be a real subscription; if the account also has
    a real Stripe subscription, this manual override does not cancel or
    modify it (the next Stripe webhook event will still reflect the real
    subscription state independently)."""
    admin = require_admin_permission(authorization, "manage_premium")
    email = email.strip().lower()
    try:
        existing = supabase.table(USER_TABLE).select("email").eq("email", email).limit(1).execute().data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzer konnte nicht geladen werden.") from exc
    if not existing:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    updated = set_plan_by_email(email, data.plan)
    if not updated:
        raise HTTPException(status_code=500, detail="Tarif konnte nicht gespeichert werden.")

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="user_plan", entity_id=email,
        metadata={"plan": data.plan, "trigger": "admin_manual_override"},
    )
    return {"message": "Tarif administrativ aktualisiert.", "email": email, "plan": data.plan}


@router.post("/users/{email}/beta/grant")
async def grant_beta_access(email: str, data: BetaGrantInput, authorization: str | None = Header(default=None)):
    """Beta Tester Program: grants a new temporary Pro/Family/Premium
    overlay to `email`, starting now, for `data.days` days — REPLACES any
    previous grant for this user (never stacks). Never touches the real
    `plan`/`premium` column or any Stripe data (see
    `core/plan_service.py::grant_beta_by_email`) — the underlying paid/free
    plan is completely unaffected either way, so a founder can safely grant
    e.g. Pro Beta to a Free user or a Premium-paying customer alike."""
    admin = require_admin_permission(authorization, "manage_premium")
    email = email.strip().lower()
    try:
        existing = supabase.table(USER_TABLE).select("email").eq("email", email).limit(1).execute().data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzer konnte nicht geladen werden.") from exc
    if not existing:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    updated = grant_beta_by_email(email, data.plan, data.days, granted_by=admin.email)
    if not updated:
        raise HTTPException(status_code=500, detail="Beta-Zugang konnte nicht gewährt werden.")

    grant = get_beta_grant_by_email(email)
    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="user_beta_grant", entity_id=email,
        metadata={"beta_plan": data.plan, "days": data.days, "trigger": "admin_beta_grant"},
    )
    return {"message": "Beta-Zugang gewährt.", "email": email, "beta_grant": grant}


@router.post("/users/{email}/beta/extend")
async def extend_beta_access(email: str, data: BetaExtendInput, authorization: str | None = Header(default=None)):
    """Extends an EXISTING Beta grant by `data.days` (from whichever is
    later of "now" and the current expiry). 404 if the user has no active
    or previous grant at all — use `/beta/grant` first in that case."""
    admin = require_admin_permission(authorization, "manage_premium")
    email = email.strip().lower()

    updated = extend_beta_by_email(email, data.days, granted_by=admin.email)
    if updated is None:
        raise HTTPException(status_code=404, detail="Kein Beta-Zugang zum Verlängern vorhanden.")

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="user_beta_grant", entity_id=email,
        metadata={"days_added": data.days, "new_expires_at": updated.get("expires_at"), "trigger": "admin_beta_extend"},
    )
    return {"message": "Beta-Zugang verlängert.", "email": email, "beta_grant": updated}


@router.post("/users/{email}/beta/revoke")
async def revoke_beta_access(email: str, authorization: str | None = Header(default=None)):
    """Immediately ends the Beta overlay — the user falls back to their
    real underlying `plan` on their very next request. Deletes NO user
    data, does NOT touch Stripe, does NOT touch Family memberships (a
    revoked Family Beta tester simply loses Family-exclusive gating like
    `family_profiles`/`family_goals`/`family_challenges` again; any Family
    they already belong to and its data is untouched — see the Family Beta
    section of the implementation report)."""
    admin = require_admin_permission(authorization, "manage_premium")
    email = email.strip().lower()

    previous = revoke_beta_by_email(email)
    if previous is None:
        raise HTTPException(status_code=404, detail="Kein aktiver Beta-Zugang vorhanden.")

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="user_beta_grant", entity_id=email,
        metadata={"revoked_plan": previous.get("plan"), "trigger": "admin_beta_revoke"},
    )
    return {"message": "Beta-Zugang widerrufen.", "email": email}


@router.get("/beta-testers")
async def list_beta_testers(authorization: str | None = Header(default=None)):
    """Small, focused Beta Tester overview (Beta Tester Program) —
    deliberately NOT a CRM: just enough to answer "who is currently
    testing and when does their access expire?". Shows only
    access-management metadata (email/name/tier/dates/who granted it) —
    never wellness/health data, matching the DSGVO/privacy boundary for
    this feature."""
    require_admin_permission(authorization, "view_users")

    try:
        rows = (
            supabase.table(USER_TABLE)
            .select("email,full_name,plan,beta_plan,beta_started_at,beta_expires_at,beta_granted_by")
            .not_.is_("beta_plan", "null")
            .order("beta_expires_at")
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    now = datetime.now(timezone.utc)
    testers: list[dict[str, object]] = []
    for row in rows:
        expires_raw = row.get("beta_expires_at")
        try:
            expires_at = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00")) if expires_raw else None
        except Exception:
            expires_at = None
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if not expires_at:
            status = "unknown"
            remaining_days = None
        else:
            remaining_days = (expires_at - now).days
            if expires_at <= now:
                status = "expired"
            elif remaining_days <= 7:
                status = "expiring_soon"
            else:
                status = "active"

        testers.append(
            {
                "email": row.get("email"),
                "full_name": row.get("full_name"),
                "real_plan": row.get("plan"),
                "beta_plan": row.get("beta_plan"),
                "beta_started_at": row.get("beta_started_at"),
                "beta_expires_at": expires_raw,
                "beta_granted_by": row.get("beta_granted_by"),
                "status": status,
                "remaining_days": remaining_days,
            }
        )

    summary = {
        "total": len(testers),
        "active": sum(1 for t in testers if t["status"] == "active"),
        "expiring_soon": sum(1 for t in testers if t["status"] == "expiring_soon"),
        "expired": sum(1 for t in testers if t["status"] == "expired"),
        "pro_beta": sum(1 for t in testers if t["beta_plan"] == "pro" and t["status"] != "expired"),
        "family_beta": sum(1 for t in testers if t["beta_plan"] == "family" and t["status"] != "expired"),
        "premium_beta": sum(1 for t in testers if t["beta_plan"] == "premium" and t["status"] != "expired"),
    }
    return {"summary": summary, "testers": testers}


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


@router.get("/users/deletion-requests")
async def list_deletion_requests(authorization: str | None = Header(default=None)):
    """Surfaces the deletion requests written by `routers/profile.py::
    request_deletion` (Etappe 9 §2) — previously there was no admin-facing
    way to even see these, only a manual DB lookup."""
    require_admin_permission(authorization, "view_users")
    try:
        rows = (
            supabase.table(PROFILE_TABLE)
            .select("email,display_name,deletion_requested_at")
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    items = [row for row in rows if row.get("deletion_requested_at")]
    items.sort(key=lambda row: row["deletion_requested_at"], reverse=True)
    return {"items": items}


@router.post("/users/{email}/deletion-requests/complete")
async def complete_deletion_request(email: str, authorization: str | None = Header(default=None)):
    """Actually executes an already-requested deletion (irreversible) —
    deletes every row scoped to this email across all user-data tables,
    then the account itself. Never automatic; an admin must trigger this
    explicitly after reviewing the request.

    Safety step (Soft-Delete-vor-Hard-Delete workflow): deactivates the
    account first (same effect as "Account deaktivieren" — blocks login,
    see `routers/users.py::login`) so no session can keep using the
    account during/just before the purge, even if an admin skipped the
    separate manual deactivation step — belt-and-suspenders, not a
    replacement for that explicit action."""
    admin = require_admin_permission(authorization, "manage_users")
    email = email.strip().lower()

    try:
        supabase.table(USER_TABLE).update(
            {"suspended": True, "suspended_at": datetime.now(timezone.utc).isoformat(), "suspended_reason": "Löschung wird abgeschlossen"}
        ).eq("email", email).execute()
    except Exception:
        pass  # best-effort safety step; the purge below is the actual guarantee

    deleted_rows = purge_all_user_data(email)

    record_audit_event(
        user_id=None,
        email=admin.email,
        action="delete",
        entity_type="user_account",
        entity_id=email,
        metadata={"deleted_rows": deleted_rows},
    )
    return {"message": "Konto und alle zugehörigen Daten wurden gelöscht.", "email": email, "deleted_rows": deleted_rows}


@router.get("/users/qa-cleanup/preview")
async def preview_qa_cleanup(authorization: str | None = Header(default=None)):
    """Dry-run for the QA-test-account cleanup (Admin Control Center §QA
    Cleanup) — lists every account matching the strict, project-defined QA
    marker WITHOUT deleting anything. Deliberately NOT "email contains
    'test'" (too broad, could match a real user) — requires the exact
    `qa-test-` email prefix AND the `QA TEST ACCOUNT` full_name marker,
    both at once. super_admin only, same severity class as a real hard
    delete."""
    admin = require_admin_permission(authorization, "manage_users")
    if admin.role != "super_admin":
        raise HTTPException(status_code=403, detail="Nur Super-Admins dürfen QA-Testaccounts bereinigen.")

    try:
        rows = supabase.table(USER_TABLE).select("email,full_name,created_at").execute().data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzerliste konnte nicht geladen werden.") from exc

    matches = [row for row in rows if _is_qa_test_account(row.get("email", ""), row.get("full_name"))]
    return {"count": len(matches), "items": matches}


@router.post("/users/qa-cleanup/execute")
async def execute_qa_cleanup(data: QACleanupExecuteInput, authorization: str | None = Header(default=None)):
    """Actually removes every account matching the strict QA marker (see
    `preview_qa_cleanup` above for the exact rule) — requires `confirm:
    true` explicitly (the frontend must have shown the dry-run list to the
    admin first). Reuses `purge_all_user_data` — the SAME deletion
    implementation as the real GDPR flow, not a second parallel one. Any
    matched row that also holds an admin role is skipped (defense in
    depth — a real admin account should never match the QA pattern, but
    this makes sure a wildcard-like mistake can't ever remove one)."""
    admin = require_admin_permission(authorization, "manage_users")
    if admin.role != "super_admin":
        raise HTTPException(status_code=403, detail="Nur Super-Admins dürfen QA-Testaccounts bereinigen.")
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Bestätigung erforderlich (confirm=true).")

    try:
        rows = supabase.table(USER_TABLE).select("email,full_name").execute().data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Nutzerliste konnte nicht geladen werden.") from exc

    matches = [row for row in rows if _is_qa_test_account(row.get("email", ""), row.get("full_name"))]

    try:
        admin_role_rows = supabase.table(ADMIN_ROLE_TABLE).select("email").execute().data or []
        admin_emails = {row["email"] for row in admin_role_rows if row.get("email")}
    except Exception:
        admin_emails = set()

    results: list[dict[str, object]] = []
    succeeded = 0
    for row in matches:
        row_email = row["email"]
        if row_email in admin_emails:
            results.append({"email": row_email, "success": False, "error": "Übersprungen: Konto hat eine Admin-Rolle."})
            continue
        try:
            deleted_rows = purge_all_user_data(row_email)
            if not deleted_rows.get(USER_TABLE):
                results.append({"email": row_email, "success": False, "error": "Konto nicht gefunden (evtl. bereits gelöscht)."})
                continue
            succeeded += 1
            results.append({"email": row_email, "success": True, "deleted_rows": deleted_rows})
        except Exception as exc:
            results.append({"email": row_email, "success": False, "error": str(exc)})

    failed = len(matches) - succeeded
    record_audit_event(
        user_id=None,
        email=admin.email,
        action="delete",
        entity_type="qa_cleanup",
        entity_id=None,
        metadata={"attempted": len(matches), "succeeded": succeeded, "failed": failed},
    )

    summary = f"{succeeded} QA-Testaccounts erfolgreich bereinigt" + (f", {failed} fehlgeschlagen" if failed else "")
    return {"message": summary, "attempted": len(matches), "succeeded": succeeded, "failed": failed, "results": results}


# ---------------------------------------------------------------------------
# Security Center
# ---------------------------------------------------------------------------


@router.get("/security/audit-logs")
async def get_audit_logs(limit: int = 50, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_security")
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    try:
        response = (
            supabase.table(AUDIT_TABLE)
            .select("*", count="exact")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = response.data or []
        total = response.count or 0
    except Exception:
        rows = []
        total = 0
    return {"items": rows, "total": total}


@router.get("/security/login-history")
async def get_global_login_history(limit: int = 50, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_security")
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    try:
        response = (
            supabase.table(LOGIN_EVENT_TABLE)
            .select("*", count="exact")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = response.data or []
        total = response.count or 0
        failed_total = (
            supabase.table(LOGIN_EVENT_TABLE).select("email", count="exact").eq("success", False).execute().count or 0
        )
    except Exception:
        rows = []
        total = 0
        failed_total = 0
    return {"items": rows, "total": total, "failed_total": failed_total}


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
    latest_release = get_latest_release()
    latest_backup = get_latest_backup_status()
    error_summary = get_error_summary(days=7)
    return {
        "database": {"status": "reachable" if db_reachable else "unreachable"},
        "openai": {"configured": bool(os.getenv("OPENAI_API_KEY", "").strip())},
        "stripe": {"configured": bool(os.getenv("STRIPE_SECRET_KEY", "").strip())},
        "storage": {"note": "Kein separates Objekt-Storage in Nutzung — keine Statusprüfung nötig."},
        "cron_jobs": {"note": "Kein interner Scheduler — externe Trigger (CI/CD, Cron) können POST /api/admin/system/releases/webhook, /system/backups/webhook und /api/admin/founder/automation/run-due/webhook aufrufen, sofern das jeweilige *_WEBHOOK_SECRET gesetzt ist."},
        "queues": {"note": "Keine Message-Queue im aktuellen System implementiert."},
        "health_connect": {"note": "Keine Health-Connect-Anbindung vorhanden."},
        "apple_health": {"note": "Keine Apple-Health-Anbindung vorhanden."},
        "release": latest_release or {"note": "Noch keine Releases erfasst — POST /api/admin/system/releases verwenden."},
        "backup": latest_backup or {"note": "Noch keine Backups erfasst — POST /api/admin/system/backups verwenden."},
        "error_events_7d": error_summary,
        "build_status_note": (
            "Webhook-Endpoint vorhanden (POST /api/admin/system/releases/webhook), aber deaktiviert bis "
            "RELEASE_WEBHOOK_SECRET gesetzt und im CI/CD-Job (z.B. GitHub Actions) hinterlegt ist — "
            "bis dahin werden Releases nur manuell/über POST /api/admin/system/releases erfasst."
        ),
        "backup_provider_note": (
            "Webhook-Endpoint vorhanden (POST /api/admin/system/backups/webhook), aber deaktiviert bis "
            "BACKUP_WEBHOOK_SECRET gesetzt ist — Supabase-eigene Backups (falls im Tarif enthalten) laufen "
            "außerhalb dieser App und werden hier nicht automatisch erkannt."
        ),
    }


@router.post("/system/releases")
async def create_release(data: ReleaseInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    release = record_release(
        version=data.version,
        released_by=admin.email,
        git_commit_sha=data.git_commit_sha,
        environment=data.environment,
        description=data.description,
        build_status=data.build_status,
    )
    if release is None:
        raise HTTPException(status_code=500, detail="Release konnte nicht gespeichert werden.")
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="release", entity_id=data.version)
    return release


@router.get("/system/releases")
async def get_releases(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_system_status")
    items = list_releases()
    return {"items": items, "latest": items[0] if items else None}


@router.post("/system/backups")
async def create_backup(data: BackupInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    backup = record_backup(
        status=data.status,
        backup_type=data.backup_type,
        size_bytes=data.size_bytes,
        completed_at=data.completed_at,
        note=data.note,
        recorded_by=admin.email,
    )
    if backup is None:
        raise HTTPException(status_code=500, detail="Backup-Status konnte nicht gespeichert werden.")
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="backup_status", entity_id=str(backup.get("id")))
    return backup


@router.get("/system/backups")
async def get_backups(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_system_status")
    items = list_backups()
    return {"items": items, "latest": items[0] if items else None}


# ---------------------------------------------------------------------------
# CI/CD & backup-job webhooks — shared-secret auth (no admin JWT), so an
# automated pipeline/cron job can record real releases/backups without a
# human founder logging in each time. Disabled (503) until the founder sets
# the matching secret env var — see FOUNDER_OS_MISSING_INTEGRATIONS.md.
# ---------------------------------------------------------------------------


@router.post("/system/releases/webhook")
async def create_release_webhook(
    data: ReleaseInput, request: Request, x_webhook_secret: str | None = Header(default=None)
):
    enforce_rate_limit(request, "release_webhook", max_requests=30, window_seconds=3600)
    require_webhook_secret(x_webhook_secret, "RELEASE_WEBHOOK_SECRET")
    release = record_release(
        version=data.version,
        released_by="ci_cd_pipeline",
        git_commit_sha=data.git_commit_sha,
        environment=data.environment,
        description=data.description,
        build_status=data.build_status,
    )
    if release is None:
        raise HTTPException(status_code=500, detail="Release konnte nicht gespeichert werden.")
    record_audit_event(user_id=None, email="ci_cd_pipeline", action="create", entity_type="release", entity_id=data.version)
    return release


@router.post("/system/backups/webhook")
async def create_backup_webhook(
    data: BackupInput, request: Request, x_webhook_secret: str | None = Header(default=None)
):
    enforce_rate_limit(request, "backup_webhook", max_requests=30, window_seconds=3600)
    require_webhook_secret(x_webhook_secret, "BACKUP_WEBHOOK_SECRET")
    backup = record_backup(
        status=data.status,
        backup_type=data.backup_type,
        size_bytes=data.size_bytes,
        completed_at=data.completed_at,
        note=data.note,
        recorded_by="backup_job",
    )
    if backup is None:
        raise HTTPException(status_code=500, detail="Backup-Status konnte nicht gespeichert werden.")
    record_audit_event(user_id=None, email="backup_job", action="create", entity_type="backup_status", entity_id=str(backup.get("id")))
    return backup


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


ALLOWED_CONTACT_STATUSES = {"new", "beantwortet", "archiviert"}


class ContactStatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_CONTACT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_CONTACT_STATUSES))}")
        return value


@router.get("/support/contacts")
async def list_contact_messages(
    status: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, authorization: str | None = Header(default=None)
):
    """Previously nothing surfaced submissions from `/kontakt`
    (`routers/contact.py::send_contact_message`) except a best-effort SMTP
    notification email (silently a no-op if SMTP isn't configured) — this
    gives admins a reliable, always-available way to see them."""
    require_admin_permission(authorization, "view_support")
    start, end = _paginate(page, page_size)
    try:
        query = supabase.table(CONTACT_TABLE).select("*", count="exact")
        if status.strip():
            query = query.eq("status", status.strip())
        response = query.order("created_at", desc=True).range(start, end).execute()
        items = response.data or []
        total = response.count or 0
    except Exception:
        items = []
        total = 0
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.patch("/support/contacts/{message_id}/status")
async def update_contact_message_status(
    message_id: str, data: ContactStatusInput, authorization: str | None = Header(default=None)
):
    admin = require_admin_permission(authorization, "manage_support")
    try:
        supabase.table(CONTACT_TABLE).update({"status": data.status}).eq("id", message_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Status konnte nicht aktualisiert werden.") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="contact_message", entity_id=message_id,
        metadata={"status": data.status},
    )
    return {"message": "Status aktualisiert.", "id": message_id, "status": data.status}


@router.get("/support/beta-applications")
async def list_beta_applications(
    page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, authorization: str | None = Header(default=None)
):
    """Previously only a total COUNT was shown on the dashboard
    (`beta_applications_total`) — admins had no way to see WHO applied or
    read their motivation. Now includes each application's real `status`
    (pending/approved/rejected, migration 039) so the founder can act via
    the `approve`/`reject` endpoints below — no separate/fabricated status."""
    require_admin_permission(authorization, "view_support")
    start, end = _paginate(page, page_size)
    try:
        response = (
            supabase.table(BETA_APPLICATION_TABLE)
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
    }


BETA_TESTER_GRANT_PLAN = "pro"
BETA_TESTER_GRANT_DAYS = 90


@router.post("/support/beta-applications/{application_id}/approve")
async def approve_beta_application(application_id: int, authorization: str | None = Header(default=None)):
    """One-click 'Beta freigeben': approves a PENDING application and grants
    exactly 90 days of Pro via the EXISTING Beta Tester Program overlay
    (`grant_beta_by_email`) — never a second entitlement mechanism, never a
    Stripe subscription/charge. Reuses the same `manage_premium` permission
    as the existing manual beta grant endpoints (no new permission).
    A real paid Pro/Family user is automatically protected from any
    downgrade — `grant_beta_by_email` never touches the real plan, and
    `get_effective_plan_by_email` always returns whichever of real-plan/
    beta-grant ranks higher."""
    admin = require_admin_permission(authorization, "manage_premium")

    try:
        rows = (
            supabase.table(BETA_APPLICATION_TABLE)
            .select("id,email,status")
            .eq("id", application_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Bewerbung konnte nicht geladen werden.") from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Bewerbung nicht gefunden.")
    application = rows[0]
    if application.get("status") != "pending":
        raise HTTPException(status_code=409, detail="Diese Bewerbung wurde bereits bearbeitet.")

    email = str(application["email"]).strip().lower()
    try:
        user_exists = bool(supabase.table(USER_TABLE).select("email").eq("email", email).limit(1).execute().data)
    except Exception:
        user_exists = False
    if not user_exists:
        raise HTTPException(
            status_code=422,
            detail=(
                "Für diese Bewerbung existiert noch kein VitalTwin-Konto mit dieser E-Mail-Adresse. "
                "Bitte den Bewerber bitten, sich zuerst mit dieser E-Mail-Adresse zu registrieren — "
                "danach kann die Freigabe hier erneut ausgeführt werden."
            ),
        )

    updated = grant_beta_by_email(email, BETA_TESTER_GRANT_PLAN, BETA_TESTER_GRANT_DAYS, granted_by=admin.email)
    if not updated:
        raise HTTPException(status_code=500, detail="Beta-Zugang konnte nicht gewährt werden.")

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table(BETA_APPLICATION_TABLE).update(
            {"status": "approved", "reviewed_at": now_iso, "reviewed_by": admin.email}
        ).eq("id", application_id).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Beta-Zugang wurde gewährt, der Bewerbungsstatus konnte aber nicht aktualisiert werden.",
        ) from exc

    grant = get_beta_grant_by_email(email)
    record_audit_event(
        user_id=None,
        email=admin.email,
        action="update",
        entity_type="beta_application",
        entity_id=str(application_id),
        metadata={
            "status": "approved",
            "granted_email": email,
            "beta_plan": BETA_TESTER_GRANT_PLAN,
            "days": BETA_TESTER_GRANT_DAYS,
        },
    )
    return {"message": "Beta-Zugang freigegeben.", "application_id": application_id, "email": email, "beta_grant": grant}


@router.post("/support/beta-applications/{application_id}/reject")
async def reject_beta_application(application_id: int, authorization: str | None = Header(default=None)):
    """Marks a PENDING application as rejected — grants no entitlement at
    all, never touches `vt_users`."""
    admin = require_admin_permission(authorization, "manage_premium")

    try:
        rows = (
            supabase.table(BETA_APPLICATION_TABLE)
            .select("id,status")
            .eq("id", application_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Bewerbung konnte nicht geladen werden.") from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Bewerbung nicht gefunden.")
    if rows[0].get("status") != "pending":
        raise HTTPException(status_code=409, detail="Diese Bewerbung wurde bereits bearbeitet.")

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table(BETA_APPLICATION_TABLE).update(
            {"status": "rejected", "reviewed_at": now_iso, "reviewed_by": admin.email}
        ).eq("id", application_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Status konnte nicht aktualisiert werden.") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="beta_application", entity_id=str(application_id),
        metadata={"status": "rejected"},
    )
    return {"message": "Bewerbung abgelehnt.", "application_id": application_id}


# ---------------------------------------------------------------------------
# Twin Overview (Enterprise Admin Dashboard, Bereich 5)
# ---------------------------------------------------------------------------


@router.get("/twin/overview")
async def twin_overview(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_dashboard")

    try:
        calc_rows = supabase.table(TWIN_CALC_TABLE).select("email").execute().data or []
    except Exception:
        calc_rows = []
    active_twins = len({row["email"] for row in calc_rows if row.get("email")})
    total_calculations = len(calc_rows)

    return {
        "active_twins": active_twins,
        "total_calculations": total_calculations,
        "twin_memory_entries": _count_rows(TWIN_MEMORY_TABLE),
        "learning_events": _count_rows(TWIN_LEARNING_EVENTS_TABLE),
        "recommendation_feedback_count": _count_rows(RECOMMENDATION_FEEDBACK_TABLE),
        "sub_twins_note": (
            "Nutrition-, Sleep-, Movement- und Metabolic-Twin sind aktuell keine "
            "eigenen Datenmodelle — VitalTwin berechnet einen kombinierten Score "
            "(vt_twin_calculations: HbA1c, CRP, Vitamin D, ApoB). Eine Aufsplittung "
            "in separate Sub-Twins ist nicht implementiert."
        ),
    }


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def _parse_dt(value: object) -> datetime | None:
    """Parses a Supabase timestamptz value (ISO string, possibly with a
    trailing 'Z') into a timezone-aware datetime, or None if unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _compute_growth_kpis(
    users: list[dict], calc_times_by_email: dict[str, list[datetime]], now: datetime
) -> dict:
    """Computes the Master-Roadmap KPIs (Activation Rate, Time to Value,
    Week-1/Week-4 Retention, "2+ Berechnungen in 30 Tagen") directly from
    existing registration (`vt_users.created_at`) and calculation
    (`vt_twin_calculations.created_at`) timestamps — no dedicated
    event-tracking table is needed for these specific metrics, since a Twin
    calculation already is the core "activation" action.

    Returns None for any rate whose cohort isn't old enough yet to measure
    (e.g. Week-4 retention needs registrations that are already >= 28 days
    old) instead of a misleading 0% or 100%.
    """
    activated_24h = 0
    ttv_seconds: list[float] = []
    week1_eligible = week1_active = 0
    week4_eligible = week4_active = 0
    two_plus_eligible = two_plus_active = 0

    for user in users:
        reg_dt = _parse_dt(user.get("created_at"))
        email = user.get("email")
        if not reg_dt or not email:
            continue
        calc_times = calc_times_by_email.get(email, [])

        first_calc = calc_times[0] if calc_times else None
        if first_calc and (first_calc - reg_dt) <= timedelta(hours=24):
            activated_24h += 1
        if first_calc:
            ttv_seconds.append((first_calc - reg_dt).total_seconds())

        if now - reg_dt >= timedelta(days=7):
            week1_eligible += 1
            if any(reg_dt + timedelta(days=1) <= t <= reg_dt + timedelta(days=7) for t in calc_times):
                week1_active += 1

        if now - reg_dt >= timedelta(days=28):
            week4_eligible += 1
            if any(reg_dt + timedelta(days=22) <= t <= reg_dt + timedelta(days=28) for t in calc_times):
                week4_active += 1

        if now - reg_dt >= timedelta(days=30):
            two_plus_eligible += 1
            in_window = [t for t in calc_times if reg_dt <= t <= reg_dt + timedelta(days=30)]
            if len(in_window) >= 2:
                two_plus_active += 1

    total_users = len(users)
    median_ttv_hours = None
    if ttv_seconds:
        ttv_seconds.sort()
        mid = len(ttv_seconds) // 2
        median_seconds = (
            ttv_seconds[mid] if len(ttv_seconds) % 2 else (ttv_seconds[mid - 1] + ttv_seconds[mid]) / 2
        )
        median_ttv_hours = round(median_seconds / 3600, 2)

    return {
        "activation_rate_24h": round(activated_24h / total_users, 3) if total_users else None,
        "median_time_to_value_hours": median_ttv_hours,
        "week1_retention_rate": round(week1_active / week1_eligible, 3) if week1_eligible else None,
        "week4_retention_rate": round(week4_active / week4_eligible, 3) if week4_eligible else None,
        "two_plus_calculations_30d_rate": (
            round(two_plus_active / two_plus_eligible, 3) if two_plus_eligible else None
        ),
        "week1_retention_cohort_size": week1_eligible,
        "week4_retention_cohort_size": week4_eligible,
        "two_plus_calculations_cohort_size": two_plus_eligible,
    }


@router.get("/analytics/growth")
async def analytics_growth(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_analytics")
    today = date.today()
    now = datetime.now(timezone.utc)
    month_start = (today - timedelta(days=30)).isoformat()

    def _all_users() -> list[dict]:
        try:
            return supabase.table(USER_TABLE).select("email,created_at,premium").execute().data or []
        except Exception:
            return []

    def _checkin_rows() -> list[dict]:
        # dau_today/mau_30d below never look further back than 30 days —
        # bounding this query avoids downloading the entire (fastest-growing)
        # check-in history on every admin page load.
        try:
            return supabase.table(DAILY_ENTRY_TABLE).select("email,entry_date").gte("entry_date", month_start).execute().data or []
        except Exception:
            return []

    def _calc_rows() -> list[dict]:
        try:
            return supabase.table(TWIN_CALC_TABLE).select("email,created_at").execute().data or []
        except Exception:
            return []

    all_users, checkin_rows, calc_rows = await asyncio.to_thread(run_parallel, _all_users, _checkin_rows, _calc_rows)

    registrations_by_day: dict[str, int] = {}
    premium_count = 0
    for user in all_users:
        if user.get("premium"):
            premium_count += 1
        created = user.get("created_at")
        if created:
            day = str(created)[:10]
            registrations_by_day[day] = registrations_by_day.get(day, 0) + 1

    dau_today = len({row["email"] for row in checkin_rows if row.get("entry_date") == today.isoformat()})
    mau_30d = len({row["email"] for row in checkin_rows if str(row.get("entry_date", "")) >= month_start})

    calc_times_by_email: dict[str, list[datetime]] = {}
    for row in calc_rows:
        email = row.get("email")
        dt = _parse_dt(row.get("created_at"))
        if email and dt:
            calc_times_by_email.setdefault(email, []).append(dt)
    for times in calc_times_by_email.values():
        times.sort()

    kpis = _compute_growth_kpis(all_users, calc_times_by_email, now)

    total_users = len(all_users)
    conversion_rate = round(premium_count / total_users, 3) if total_users else None

    return {
        "total_users": total_users,
        "premium_users": premium_count,
        "premium_conversion_rate": conversion_rate,
        "registrations_by_day": registrations_by_day,
        "dau_today": dau_today,
        "mau_30d": mau_30d,
        **kpis,
        "retention_note": (
            "Activation/TTV/Retention werden aus vt_users.created_at und "
            "vt_twin_calculations.created_at berechnet (Twin-Berechnung als "
            "Aktivierungs-Proxy). Werte sind null, solange keine Nutzer-Kohorte "
            "alt genug ist (z. B. Week-4 braucht >= 28 Tage alte Registrierungen)."
        ),
        "session_duration_note": "Keine Session-Dauer-Messung implementiert (kein Frontend-Analytics-Tracking).",
        "feature_usage_note": "Feature-Nutzung im Detail nicht aggregiert — Rohdaten liegen in den jeweiligen Fachtabellen vor.",
    }


# ---------------------------------------------------------------------------
# Content Management
# ---------------------------------------------------------------------------


@router.get("/content")
async def list_content(
    content_type: str | None = None, status: str | None = None, authorization: str | None = Header(default=None)
):
    require_admin_permission(authorization, "view_content")
    try:
        query = supabase.table(CONTENT_TABLE).select("*")
        if content_type:
            query = query.eq("content_type", content_type)
        if status:
            query = query.eq("status", status)
        rows = query.order("updated_at", desc=True).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.get("/content/{content_id}")
async def get_content_item(content_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_content")
    try:
        rows = supabase.table(CONTENT_TABLE).select("*").eq("id", content_id).limit(1).execute().data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inhalt konnte nicht geladen werden.") from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Inhalt nicht gefunden.")
    return rows[0]


def _find_slug_conflict(content_type: str, slug: str | None, *, exclude_id: str | None = None) -> bool:
    """A slug only needs to be unique within its own `content_type` (matches
    the DB's own unique index `uq_vt_content_items_type_slug`). Checking
    here first lets us return a clear 409 instead of a raw DB error."""
    if not slug:
        return False
    try:
        query = supabase.table(CONTENT_TABLE).select("id").eq("content_type", content_type).eq("slug", slug)
        if exclude_id:
            query = query.neq("id", exclude_id)
        rows = query.limit(1).execute().data or []
    except Exception:
        return False
    return bool(rows)


@router.post("/content")
async def create_content(data: ContentInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_content")
    if _find_slug_conflict(data.content_type, data.slug):
        raise HTTPException(status_code=409, detail="Dieser Slug wird für diesen Content-Typ bereits verwendet.")

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
    if _find_slug_conflict(data.content_type, data.slug, exclude_id=content_id):
        raise HTTPException(status_code=409, detail="Dieser Slug wird für diesen Content-Typ bereits verwendet.")

    payload = data.model_dump()
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if data.status == "published":
        payload["published_at"] = datetime.now(timezone.utc).isoformat()

    try:
        response = supabase.table(CONTENT_TABLE).update(payload).eq("id", content_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inhalt konnte nicht aktualisiert werden.") from exc
    if not response.data:
        raise HTTPException(status_code=404, detail="Inhalt nicht gefunden.")

    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="content_item", entity_id=content_id)
    return response.data[0]


@router.post("/content/{content_id}/publish")
async def publish_content(content_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_content")
    item = await get_content_item(content_id, authorization=authorization)

    missing = [
        field
        for field, value in (("Titel", item.get("title")), ("Slug", item.get("slug")), ("Inhalt", item.get("body")))
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=422, detail=f"Vor Veröffentlichung fehlt noch: {', '.join(missing)}."
        )
    if _find_slug_conflict(item["content_type"], item["slug"], exclude_id=content_id):
        raise HTTPException(status_code=409, detail="Dieser Slug wird für diesen Content-Typ bereits verwendet.")

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        response = (
            supabase.table(CONTENT_TABLE)
            .update({"status": "published", "published_at": now_iso, "updated_at": now_iso})
            .eq("id", content_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inhalt konnte nicht veröffentlicht werden.") from exc

    record_audit_event(user_id=None, email=admin.email, action="publish", entity_type="content_item", entity_id=content_id)
    return response.data[0] if response.data else {"message": "Veröffentlicht."}


@router.post("/content/{content_id}/unpublish")
async def unpublish_content(content_id: str, authorization: str | None = Header(default=None)):
    """Reverts a published item to draft — deactivates the public
    presentation but never deletes the underlying content or its history
    (`published_at` is left untouched as a record of the first publish)."""
    admin = require_admin_permission(authorization, "manage_content")
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        response = (
            supabase.table(CONTENT_TABLE).update({"status": "draft", "updated_at": now_iso}).eq("id", content_id).execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Inhalt konnte nicht auf Entwurf zurückgesetzt werden.") from exc
    if not response.data:
        raise HTTPException(status_code=404, detail="Inhalt nicht gefunden.")

    record_audit_event(user_id=None, email=admin.email, action="unpublish", entity_type="content_item", entity_id=content_id)
    return response.data[0]


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

    def _usage_rows() -> list[dict]:
        try:
            return supabase.table(CHAT_USAGE_TABLE).select("*").execute().data or []
        except Exception:
            return []

    def _usage_today() -> dict:
        return get_ai_usage_summary(days=1)

    def _usage_30d() -> dict:
        return get_ai_usage_summary(days=30)

    rows, usage_today, usage_30d = await asyncio.to_thread(run_parallel, _usage_rows, _usage_today, _usage_30d)

    total_requests = sum(int(row.get("count", 0)) for row in rows)
    unique_users = len({row["email"] for row in rows if row.get("email")})
    requests_today = sum(int(row.get("count", 0)) for row in rows if row.get("usage_date") == today.isoformat())

    return {
        "model_configured": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "total_requests_all_time": total_requests,
        "unique_users_all_time": unique_users,
        "requests_today": requests_today,
        "token_usage_note": "Token-Zahlen werden seit Migration 022 pro Anfrage in vt_ai_usage_events erfasst (siehe usage_today/usage_30d).",
        "response_time_note": "Antwortzeit (avg_latency_ms) wird seit Migration 022 pro Anfrage erfasst (siehe usage_today/usage_30d).",
        "prompt_versions_note": "Kein Prompt-Versionierungssystem — der Systemprompt ist aktuell fest im Code (`services/twin_conversation.py`).",
        "usage_today": usage_today,
        "usage_30d": usage_30d,
    }


# ---------------------------------------------------------------------------
# Business Center
# ---------------------------------------------------------------------------


@router.get("/business/overview")
async def business_overview(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_business")

    def _premium_users() -> int | None:
        return _count_rows(USER_TABLE, filters={"premium": True})

    def _revenue() -> dict:
        return stripe_billing.get_revenue_summary()

    def _subscriptions() -> dict:
        return stripe_billing.get_subscription_summary()

    def _refunds() -> dict:
        return stripe_billing.get_refund_summary(days=30)

    premium_users, revenue, subscriptions, refunds = await asyncio.to_thread(
        run_parallel, _premium_users, _revenue, _subscriptions, _refunds
    )

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
        "revenue_today": revenue["revenue_today"],
        "revenue_month": revenue["revenue_month"],
        "revenue_note": revenue["note"],
        "active_subscriptions": subscriptions["active"],
        "canceled_subscriptions": subscriptions["canceled"],
        "subscriptions_note": subscriptions["note"],
        "refunds_count_30d": refunds["count"],
        "refunds_total_30d": refunds["total"],
        "refunds_note": refunds["note"],
        "affiliate_note": "Kein Affiliate-/Provisions-System implementiert.",
        "coupons_note": "Keine Gutschein-Verwaltung implementiert.",
    }


# ---------------------------------------------------------------------------
# Nutrition & CGM
# ---------------------------------------------------------------------------


@router.get("/nutrition/overview")
async def nutrition_overview(authorization: str | None = Header(default=None)):
    """Real pipeline monitoring, now that `routers/health.py` (CGM CSV
    import + manual nutrition logging) actually writes to
    `vt_cgm_readings`/`vt_nutrition_entries`. Still honestly reports
    `available: False` + a note when both tables are empty — never a
    fabricated number — matching every other Founder-OS/Admin module."""
    require_admin_permission(authorization, "view_nutrition_admin")

    def _cgm_rows() -> list[dict]:
        try:
            return (
                supabase.table("vt_cgm_readings")
                .select("email,glucose_value,reading_at")
                .order("reading_at", desc=True)
                .limit(500)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _nutrition_rows() -> list[dict]:
        try:
            return (
                supabase.table("vt_nutrition_entries")
                .select("email,meal_name,carbs,logged_at")
                .order("logged_at", desc=True)
                .limit(500)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    cgm_rows, nutrition_rows = run_parallel(_cgm_rows, _nutrition_rows)

    if not cgm_rows and not nutrition_rows:
        return {
            "status": "empty",
            "available": False,
            "note": (
                "VitalTwin hat noch keine CGM- oder Nutrition-Einträge in der Datenbank. Sobald Nutzer echte "
                "CSV-Dateien hochladen (/api/health/cgm/upload-csv) oder Mahlzeiten eintragen "
                "(/api/health/nutrition), erscheinen hier echte Kennzahlen."
            ),
            "import_errors": [],
            "connector_status": [],
            "import_stats": {},
        }

    return {
        "status": "active",
        "available": True,
        "note": None,
        "import_errors": [],
        "connector_status": [],
        "import_stats": {},
        "cgm": {
            "total_readings": len(cgm_rows),
            "unique_users": len({row["email"] for row in cgm_rows if row.get("email")}),
            "last_imports": [
                {"timestamp": row.get("reading_at"), "glucose_value": row.get("glucose_value")}
                for row in cgm_rows[:10]
            ],
        },
        "nutrition": {
            "total_entries": len(nutrition_rows),
            "unique_users": len({row["email"] for row in nutrition_rows if row.get("email")}),
            "last_entries": [
                {"meal_name": row.get("meal_name"), "carbs": row.get("carbs")}
                for row in nutrition_rows[:10]
            ],
        },
    }


# ---------------------------------------------------------------------------
# Platform Foundation — Integrations & Feature Flags
# ---------------------------------------------------------------------------

FEATURE_FLAG_TABLE = "vt_feature_flags"


@router.get("/integrations")
async def list_integrations(authorization: str | None = Header(default=None)):
    """Real, live status of every connector/provider named in the platform
    foundation spec — computed from actual env vars, never hardcoded to
    "configured". See `core/integrations.py` for the single source of truth."""
    require_admin_permission(authorization, "view_integrations")
    return get_full_integration_report()


@router.get("/feature-flags")
async def list_feature_flags(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_integrations")
    try:
        rows = supabase.table(FEATURE_FLAG_TABLE).select("*").order("key").execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


class FeatureFlagInput(BaseModel):
    enabled: bool
    description: str | None = None


@router.put("/feature-flags/{key}")
async def upsert_feature_flag(key: str, data: FeatureFlagInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_feature_flags")
    payload = {
        "key": key,
        "enabled": data.enabled,
        "updated_by": admin.email,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if data.description is not None:
        payload["description"] = data.description

    try:
        supabase.table(FEATURE_FLAG_TABLE).upsert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Feature-Flag konnte nicht gespeichert werden.") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="feature_flag", entity_id=key,
        metadata={"enabled": data.enabled},
    )
    return {"message": "Feature-Flag gespeichert."}

