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
    "automation_manager",
    "executive_analyst",
    "documentation_editor",
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
    "view_integrations",
    "manage_feature_flags",
    "view_support",
    "manage_support",
    "view_affiliate",
    "manage_affiliate",
    "view_founder_os",
    "manage_founder_os",
    "view_automation_engine",
    "manage_automation_engine",
    "view_ceo_intelligence",
    "manage_ceo_intelligence",
    "view_documentation",
    "manage_documentation",
    "view_founder_autopilot",
    "manage_founder_autopilot",
    "view_accounting",
    "manage_accounting",
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
# - automation_manager: Submodule G (Automation Engine) ONLY — an explicit,
#   narrow role for someone the founder trusts to build/maintain automation
#   rules, without granting every other admin capability. Deliberately NOT
#   part of `admin`'s automatic full grant (see the exclusion below) — per
#   the Submodule G spec ("Normale Admins dürfen nicht automatisch Zugriff
#   erhalten"), because this module can execute real, non-trivial actions
#   (pausing affiliate products, creating approvals/tasks). Only
#   `super_admin` (== the founder in this single-founder app, consistent
#   with the existing "founder-only" convention from Release F1 onward)
#   automatically has it; `admin` must be explicitly upgraded to
#   `automation_manager` or `super_admin` by the founder to gain it.
# - executive_analyst: Submodule H (CEO Intelligence) ONLY, READ-ONLY —
#   per that spec's explicit "ausdrücklich freigegebene executive_analyst-
#   Rolle mit Leserechten". Only `super_admin` may ever *change* strategic
#   goals, save scenarios, send decisions to the Approval Center, or export
#   data (`manage_ceo_intelligence` is granted to no other role at all,
#   including `admin` — same reasoning as Submodule G: this module
#   surfaces sensitive, aggregated company-wide business data).
# - documentation_editor: Submodule I (Auto Documentation) ONLY — per that
#   spec's "eingeschränkter documentation_editor". May prepare drafts,
#   trigger generation runs, and propose changes, but the founder-only
#   actions (approving protected documents, publishing public release
#   notes, changing write-area policy, final archival) are additionally
#   gated by `admin.role == "super_admin"` inside the router, not by
#   permission alone — `developer` also gets both Submodule I permissions
#   (explicit, deliberate grant per spec: "ausdrücklich freigegebener
#   developer"), since technical staff routinely maintain documentation.
# - Submodule J (Founder Autopilot): the spec is stricter than every prior
#   submodule — ONLY `super_admin` may ever configure it at all (no
#   narrow "autopilot_manager"-style role exists, unlike G/H/I).
#   `executive_analyst` additionally gets `view_founder_autopilot`
#   (read-only, same role reused across H and J per spec's explicit
#   "ausdrücklich freigegebene executive_analyst-Rolle").
# - Accounting (Buchhaltungs-Grundlage, GoBD/Steuerberater-Handover,
#   2026-08-21): same stricter-than-usual treatment as CEO Intelligence/
#   Autopilot, and for the same reason plus one more -- this surfaces real
#   Stripe+AdSense revenue data prepared for direct handover to a tax
#   advisor, so `admin` does NOT get it automatically either. Only
#   `super_admin` has `view_accounting`/`manage_accounting` by default;
#   widen this deliberately (e.g. for a future bookkeeper-admin role) by
#   editing this dict, not by loosening the permission check itself.
ROLE_PERMISSIONS: dict[AdminRole, frozenset[Permission]] = {
    "super_admin": frozenset(_ALL_PERMISSIONS),  # type: ignore[arg-type]
    "admin": frozenset(
        _ALL_PERMISSIONS
        - {
            "manage_roles", "manage_security",
            "view_automation_engine", "manage_automation_engine",
            "view_ceo_intelligence", "manage_ceo_intelligence",
            "view_documentation", "manage_documentation",
            "view_founder_autopilot", "manage_founder_autopilot",
            "view_accounting", "manage_accounting",
        }
    ),  # type: ignore[arg-type]
    "automation_manager": frozenset({"view_automation_engine", "manage_automation_engine"}),
    "executive_analyst": frozenset({"view_ceo_intelligence", "view_founder_autopilot"}),
    "documentation_editor": frozenset({"view_documentation", "manage_documentation"}),
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
            "view_integrations",
            "view_affiliate",
            "view_automation_engine",
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
            "view_integrations",
            "view_documentation",
            "manage_documentation",
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
