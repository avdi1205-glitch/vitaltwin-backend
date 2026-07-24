"""Role-Based Access Control (RBAC) for the Admin Control Center.

VitalTwin Enterprise Release — Admin Control Center 1.0.

Design principles:

- **Absence = not an admin.** A user is an admin if and only if a row for
  their `email` exists in `vt_admin_roles`. There is no implicit default
  role, no "everyone is a viewer" fallback.
- **The permission matrix lives in code, not in the database**
  (`ROLE_PERMISSIONS` below) — granting a new capability to a role never
  requires a migration, only a code review/deploy. The database only ever
  stores *which role* a given admin has.
- **Fine-grained permissions, not just roles.** Endpoints check a specific
  `Permission`, never a raw role name — this keeps `routers/admin.py` free
  of scattered `if role == "admin"` checks and makes the actual access
  model auditable in one place (this file).
- **Manual dependency calls, not `fastapi.Depends`** — matches the
  established convention in every other router in this codebase
  (`core/auth.py::require_email` is called directly inside each endpoint,
  not injected via `Depends`). `require_admin_permission` follows the same
  shape for consistency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException

from .auth import require_email as _require_email
from .supabase import supabase

ADMIN_ROLE_TABLE = "vt_admin_roles"

AdminRole = Literal[
    "super_admin",
    "admin",
    "support",
    "moderator",
    "editor",
    "analyst",
    "developer",
]

Permission = Literal[
    "view_dashboard",
    "view_users",
    "manage_users",
    "manage_roles",
    "manage_premium",
    "view_consents",
    "view_login_history",
    "view_content",
    "manage_content",
    "view_nutrition_admin",
    "view_ai_usage",
    "manage_ai_settings",
    "view_business",
    "manage_business",
    "view_analytics",
    "view_security",
    "manage_security",
    "view_system_status",
    "view_support",
    "manage_support",
]

_ALL_PERMISSIONS: frozenset[str] = frozenset(Permission.__args__)  # type: ignore[attr-defined]

# --- The permission matrix — the single source of truth for RBAC ----------
#
# Rationale per role (see docs/ADMIN_ARCHITECTURE.md §2 for the full table):
#
# - super_admin: everything, including the two "power" permissions no other
#   role gets — `manage_roles` (granting/revoking admin access itself, to
#   avoid privilege-escalation loops) and `manage_security` (changing
#   security-critical configuration).
# - admin: full day-to-day operational access, but cannot grant roles or
#   change security configuration — that stays with super_admin only.
# - support: user-facing operational support (search/view/suspend users,
#   see consents/login history to help with account issues, support
#   tickets) — no content, no business/AI configuration.
# - moderator: content moderation + support tickets + read-only user lookup
#   (to see context behind a report) — no premium/role/security changes.
# - editor: content only (blog/FAQ/landing/help/notifications) — no access
#   to any user data whatsoever.
# - analyst: read-only dashboards/analytics/business/AI-usage numbers — no
#   ability to change anything.
# - developer: system/security status (read-only) + AI configuration
#   (models/prompts) — the two areas an engineer actually needs day to day.
ROLE_PERMISSIONS: dict[AdminRole, frozenset[Permission]] = {
    "super_admin": frozenset(_ALL_PERMISSIONS),  # type: ignore[arg-type]
    "admin": frozenset(_ALL_PERMISSIONS - {"manage_roles", "manage_security"}),  # type: ignore[arg-type]
    "support": frozenset(
        {
            "view_dashboard",
            "view_users",
            "manage_users",
            "view_consents",
            "view_login_history",
            "view_support",
            "manage_support",
            "view_system_status",
        }
    ),
    "moderator": frozenset(
        {
            "view_dashboard",
            "view_users",
            "view_content",
            "manage_content",
            "view_support",
            "manage_support",
        }
    ),
    "editor": frozenset({"view_content", "manage_content"}),
    "analyst": frozenset(
        {
            "view_dashboard",
            "view_analytics",
            "view_ai_usage",
            "view_business",
            "view_system_status",
        }
    ),
    "developer": frozenset(
        {
            "view_dashboard",
            "view_system_status",
            "view_security",
            "view_ai_usage",
            "manage_ai_settings",
            "view_content",
        }
    ),
}


@dataclass(frozen=True)
class AdminPrincipal:
    """The authenticated admin principal for the current request — mirrors
    `core/auth.py::CurrentUser`, but for the admin surface."""

    email: str
    role: AdminRole


def get_admin_role(email: str) -> AdminRole | None:
    """Looks up the admin role for an email, or `None` if this user is not
    an admin at all. Never raises — a lookup failure (e.g. unreachable
    database) must never widen access, so it is treated identically to
    "not an admin"."""
    try:
        response = supabase.table(ADMIN_ROLE_TABLE).select("role").eq("email", email).limit(1).execute()
        rows = response.data or []
    except Exception:
        return None
    if not rows:
        return None
    role = rows[0].get("role")
    return role if role in ROLE_PERMISSIONS else None


def role_has_permission(role: AdminRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def require_admin(authorization: str | None) -> AdminPrincipal:
    """Like `require_admin_permission`, but does not require any specific
    permission — any admin role qualifies. Used by `GET /api/admin/me` so
    the frontend can discover its own role/permission set once at login and
    build an RBAC-aware navigation, instead of probing every endpoint."""
    email = _require_email(authorization)
    role = get_admin_role(email)
    if role is None:
        raise HTTPException(status_code=403, detail="Kein Admin-Zugriff für dieses Konto.")
    return AdminPrincipal(email=email, role=role)


def require_admin_permission(authorization: str | None, permission: Permission) -> AdminPrincipal:
    """The one function every admin endpoint calls first. Raises:

    - `401` if the caller isn't authenticated at all (via `require_email`).
    - `403` if the caller is authenticated but is not an admin, or is an
      admin without the required permission.

    `403` (not `404`) is deliberate here — unlike the "does this record
    belong to another user" ownership checks elsewhere in this codebase
    (see `core/auth.py::assert_owns`), a permission check is not about
    hiding whether a *resource* exists; the caller already knows they're
    calling an admin API. Standard REST semantics apply: `401` = who are
    you, `403` = I know who you are, you may not do this.
    """
    email = _require_email(authorization)
    role = get_admin_role(email)
    if role is None:
        raise HTTPException(status_code=403, detail="Kein Admin-Zugriff für dieses Konto.")
    if not role_has_permission(role, permission):
        raise HTTPException(status_code=403, detail="Fehlende Berechtigung für diese Aktion.")
    return AdminPrincipal(email=email, role=role)
