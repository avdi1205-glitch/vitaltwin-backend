"""Central plan/feature entitlement service (VitalTwin Plan System).

Single source of truth for "which plan does this user have" and "does that
plan include feature X" — replaces scattered `if premium`/`if plan == ...`
checks that used to live in individual routers.

BACKGROUND: `vt_users` historically only had a boolean `premium` column,
so Pro/Family subscribers were technically indistinguishable from Premium
everywhere in the backend (documented at length in earlier session notes
and confirmed by a live end-to-end test). Migration
`027_plan_field_foundation.sql` adds a real `plan` column
(`free|premium|pro|family`). This module is the only place that reads or
writes that column — routers should call `get_plan_by_email()` /
`has_feature()` instead of touching `vt_users.plan` directly.

TRANSITION SAFETY: the legacy `premium` boolean is kept in sync by
`set_plan_by_email()` (writes both columns) and by
`routers/users.py::set_premium_by_email()` (now plan-aware — see its
docstring). Existing code that still only reads the boolean (`
is_premium_by_email()`) keeps working unchanged.

FEATURE HIERARCHY (Block 3 of the plan-architecture task): built as
explicit supersets so "Pro never gets less than Premium" and "Family never
gets less than Premium for general wellness features" are structural
guarantees, not something that can be forgotten in a single `if` check.
Only features that are REALLY enforced server-side today are listed here —
see `PLAN_ARCHITECTURE_REPORT.md` for the honest list of what is NOT yet
real for Pro/Family.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from .supabase import supabase
from .plans import PLAN_PRICE_ENV_MAP

PlanId = Literal["free", "premium", "pro", "family"]
VALID_PLANS: frozenset[str] = frozenset({"free", "premium", "pro", "family"})

USER_TABLE = "vt_users"

# Beta Tester Program (admin-controlled, time-limited Pro/Family/Premium
# overlay) — a Beta grant can only ever be one of the three PAID tiers
# (never "free", nothing to grant there).
BETA_PLAN_VALUES: frozenset[str] = frozenset({"premium", "pro", "family"})
_PLAN_RANK: dict[str, int] = {"free": 0, "premium": 1, "pro": 2, "family": 3}

# ---------------------------------------------------------------------------
# Feature hierarchy — explicit supersets, not a numeric "rank" comparison,
# so adding a Pro- or Family-exclusive feature later can never accidentally
# remove something Premium already has.
# ---------------------------------------------------------------------------
_FREE_FEATURES: frozenset[str] = frozenset({
    "basic_dashboard",
    "basic_history",
})

# "detailed_wellness" and "weekly_reports" gate ONLY the customer-facing
# router endpoints (profile.py::get_personal_baseline,
# daily_planning.py::get_weekly_reflection) — never the shared service
# functions they call (`build_personal_baseline_report`/
# `compute_weekly_reflection`/`compute_trend`), which stay reusable by
# Free's own core trends and by other already-gated Pro/Family features
# (advanced_twin_overview.py, thirty_day_report.py both call the baseline
# function directly, bypassing this endpoint entirely). Two other
# Premium pricing bullets were deliberately left WITHOUT a new gate after
# inspection (see PREMIUM_ENTITLEMENT_BOUNDARIES notes in profile.py/
# daily_planning.py for the full reasoning):
# - "Schlaf-, Stress- und Erholungsübersicht" — the base 7d/30d trends
#   endpoint (GET /api/profile/trends) is Free's OWN existing core
#   feature ("Verlauf bis zu 30 Tage"); the only genuinely Premium-
#   exclusive part of that endpoint (the 90-day window) is already
#   gated by "extended_history" — adding a second gate would either
#   duplicate that logic or break Free's legitimate access.
# - "Individuelle Tagesziele" — basic goal creation/viewing is a
#   pre-existing Free-tier capability (`vt_wellness_goals`, migration
#   001), not something introduced by the Premium tier; the genuinely
#   Premium-exclusive goal capability is "multiple_goals" (Pro/Family
#   only, already enforced), which is unrelated to what this bullet
#   describes for Premium specifically.
_PREMIUM_FEATURES: frozenset[str] = _FREE_FEATURES | frozenset({
    "extended_history",  # "Erweiterter Verlauf" — GET /api/profile/trends 90d window
    "cgm_nutrition",  # CGM-Import + Ernährungstagebuch (routers/health.py)
    "detailed_wellness",  # "Ausführlichere Wellness-Auswertungen" — GET /api/profile/baseline
    "weekly_reports",  # "Wochenberichte" — GET /api/planning/weekly
    "google_health",  # "Automatische Gesundheitsdaten über Google Health" — routers/google_health.py
})

# "multiple_goals" (pricing page: "Mehrere persönliche Ziele"),
# "lifestyle_simulation" (pricing page: "Lifestyle-Simulationen"),
# "extended_reports" (pricing page: "Erweiterte Berichte"), and
# "advanced_digital_twin" (pricing page: "Vollständiger erweiterter
# digitaler Zwilling") are the only genuinely enforced Pro-exclusive
# features so far — see profile.py::create_goal/::update_goal,
# ::simulate_lifestyle_change, ::get_thirty_day_report, and
# ::get_advanced_twin_overview. Everything else Pro-exclusive is still
# `coming_soon` (see PLAN_ARCHITECTURE_REPORT.md), which remains the
# correct, honest state.
_PRO_FEATURES: frozenset[str] = _PREMIUM_FEATURES | frozenset({
    "multiple_goals",
    "lifestyle_simulation",
    "extended_reports",
    "advanced_digital_twin",
})

# Family is >= Pro for general wellness features (no real family-sharing
# backend exists yet, see PLAN_ARCHITECTURE_REPORT.md, but a Family
# subscriber should never have FEWER individual-wellness features than Pro).
# "family_profiles" (pricing page: "Bis zu 6 eigenständige Profile") is the
# first genuinely enforced Family-EXCLUSIVE feature — see
# routers/family.py::create_family. It deliberately grants ONLY the right
# to create/own a Family membership group, NOT shared wellness-data
# visibility (still not built, see routers/family.py module docstring).
# "family_goals" gates only CREATING a Family Goal (routers/family.py::
# create_family_goal) — joining/viewing/updating own progress only
# requires being an active member of the family itself (any personal
# plan), never a second personal-plan check.
# "family_challenges" gates only CREATING a Family Challenge
# (routers/family.py::create_family_challenge) — same pattern as
# family_goals: joining/viewing/updating own progress only requires being
# an active member of the family itself, never a second personal-plan
# check.
_FAMILY_FEATURES: frozenset[str] = _PREMIUM_FEATURES | frozenset({
    "multiple_goals",
    "lifestyle_simulation",
    "extended_reports",
    "advanced_digital_twin",
    "family_profiles",
    "family_goals",
    "family_challenges",
})

# Free/Premium keep today's pre-existing behavior of exactly one
# simultaneously active goal (matches Premium's own pricing bullet
# "Individuelle Tagesziele", singular) — Pro/Family get `multiple_goals`
# (unlimited). Never retroactively touches goals a user already created.
FREE_TIER_MAX_ACTIVE_GOALS = 1

# "Bis zu 6 eigenständige Profile" (pricing page) — the owner counts as one
# of the 6 (auto-added as an 'active' member with role='owner' on create).
MAX_FAMILY_MEMBERS = 6

FEATURE_SETS: dict[PlanId, frozenset[str]] = {
    "free": _FREE_FEATURES,
    "premium": _PREMIUM_FEATURES,
    "pro": _PRO_FEATURES,
    "family": _FAMILY_FEATURES,
}


def _normalize_plan(value: object) -> PlanId:
    text = str(value).strip().lower() if value else "free"
    return text if text in VALID_PLANS else "free"  # type: ignore[return-value]


def normalize_plan_row(row: dict[str, object] | None) -> PlanId:
    """Same normalization/fallback rule as `get_plan_by_email`, but for a
    row the CALLER already fetched (e.g. an admin list endpoint that
    selected `plan,premium` for many users in one query) — avoids an N+1
    per-row DB round trip. Use this whenever the row is already in hand;
    use `get_plan_by_email` only when you don't already have it."""
    if not row:
        return "free"
    plan = row.get("plan")
    if plan:
        return _normalize_plan(plan)
    return "premium" if bool(row.get("premium", False)) else "free"


def get_plan_by_email(email: str) -> PlanId:
    """Reads the real, current plan for `email` directly from the database
    (deliberately not cached — this is the security-relevant read path, and
    an out-of-process write, e.g. an admin action or a webhook handled by a
    different request, must be visible immediately here). Falls back to the
    legacy `premium` boolean if `plan` is somehow missing/null (should not
    happen after migration 027, but defensive), and to "free" if the user
    does not exist at all — never raises for an unknown email.

    Defensive two-step select: if migration 027 has not been run in
    Supabase yet, selecting a `plan` column that does not exist would make
    Postgrest reject the whole query — falls back to selecting only
    `premium` so this never regresses existing premium/free behavior while
    the migration is still pending."""
    normalized_email = email.strip().lower()
    try:
        response = (
            supabase.table(USER_TABLE)
            .select("plan,premium")
            .eq("email", normalized_email)
            .limit(1)
            .execute()
        )
        rows = response.data or []
    except Exception:
        rows = None

    if rows is None:
        try:
            response = (
                supabase.table(USER_TABLE)
                .select("premium")
                .eq("email", normalized_email)
                .limit(1)
                .execute()
            )
            rows = response.data or []
        except Exception:
            return "free"

    if not rows:
        return "free"

    return normalize_plan_row(rows[0])


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _fetch_beta_columns(email: str) -> dict[str, object]:
    """Defensive read of the 4 `beta_*` columns (migration 034) — same
    graceful-degradation rule as `get_plan_by_email`: if the migration has
    not been run in Supabase yet, PostgREST rejects the whole select and
    this returns `{}` (== no active grant) instead of raising, so the rest
    of the app keeps working exactly as before the migration exists."""
    normalized_email = email.strip().lower()
    try:
        response = (
            supabase.table(USER_TABLE)
            .select("beta_plan,beta_started_at,beta_expires_at,beta_granted_by")
            .eq("email", normalized_email)
            .limit(1)
            .execute()
        )
        rows = response.data or []
    except Exception:
        return {}
    return dict(rows[0]) if rows else {}


def get_beta_grant_by_email(email: str) -> dict[str, object] | None:
    """Current Beta Tester Program grant state for `email`, or `None` if no
    grant exists (never granted, or fully revoked). This is a pure READ —
    it does not decide feature access by itself (see
    `get_effective_plan_by_email`) — used by `/api/users/me` (customer
    "Pro · Beta-Tester" label) and the admin user-detail endpoint (Beta
    Tester panel: tier/start/expiry/remaining days)."""
    row = _fetch_beta_columns(email)
    beta_plan = row.get("beta_plan")
    if not beta_plan:
        return None

    expires_at_raw = row.get("beta_expires_at")
    expires_at = _parse_iso(expires_at_raw)
    now = datetime.now(timezone.utc)
    active = bool(expires_at and expires_at > now)
    remaining_days = max(0, (expires_at - now).days) if (active and expires_at) else 0

    return {
        "plan": beta_plan,
        "started_at": row.get("beta_started_at"),
        "expires_at": expires_at_raw,
        "granted_by": row.get("beta_granted_by"),
        "active": active,
        "remaining_days": remaining_days,
    }


def get_effective_plan_by_email(email: str) -> PlanId:
    """The plan that ACTUALLY governs feature access right now: the higher
    (by feature-hierarchy rank) of the user's real underlying `plan` and an
    active, non-expired Beta grant — "valid paid subscription OR active
    temporary Beta grant". Never returns a plan below the real underlying
    plan (a Beta grant only ever ADDS access, never removes it) and never
    persists/mutates anything — a pure read-time overlay. This is the ONE
    function both `has_feature()` (real backend gating) and
    `/api/users/me` (so existing client-side `profile.plan === 'pro'`
    checks already sprinkled across the frontend work correctly for Beta
    testers too, with zero extra per-page frontend changes) should use."""
    real_plan = get_plan_by_email(email)
    grant = get_beta_grant_by_email(email)
    if grant and grant["active"]:
        beta_plan = grant["plan"]
        if _PLAN_RANK.get(str(beta_plan), 0) > _PLAN_RANK.get(real_plan, 0):
            return beta_plan  # type: ignore[return-value]
    return real_plan


def grant_beta_by_email(email: str, plan: str, days: int, granted_by: str) -> bool:
    """Admin-only action (authorization enforced by the calling router via
    `manage_premium`, never here). Sets a brand-new temporary grant,
    starting now — always REPLACES any previous grant for this user
    (never stacks/extends implicitly; use `extend_beta_by_email` for that).
    NEVER touches `vt_users.plan`/`premium` (the real paid/free plan) or
    any Stripe data — purely the 4 `beta_*` overlay columns, so a real
    paying Pro/Family subscriber's own plan is completely unaffected
    either way, and no fake Stripe subscription is ever created."""
    if plan not in BETA_PLAN_VALUES:
        raise ValueError(f"Ung\u00fcltiger Beta-Tarif: {plan!r}")
    if days <= 0:
        raise ValueError("Beta-Zugang ben\u00f6tigt eine positive Anzahl Tage.")

    normalized_email = email.strip().lower()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=days)

    try:
        response = (
            supabase.table(USER_TABLE)
            .update(
                {
                    "beta_plan": plan,
                    "beta_started_at": now.isoformat(),
                    "beta_expires_at": expires_at.isoformat(),
                    "beta_granted_by": granted_by.strip().lower(),
                }
            )
            .eq("email", normalized_email)
            .execute()
        )
    except Exception:
        return False
    return bool(response.data)


def extend_beta_by_email(email: str, days: int, granted_by: str) -> dict[str, object] | None:
    """Extends an EXISTING grant by `days`, counted from whichever is later
    of "now" and the current expiry (extending an already-expired grant
    never silently backdates it; extending a still-active one simply
    pushes the end date further out). Returns `None` if the user has no
    `beta_plan` set at all — callers should use `grant_beta_by_email`
    instead in that case (there is nothing to extend)."""
    if days <= 0:
        raise ValueError("Verl\u00e4ngerung ben\u00f6tigt eine positive Anzahl Tage.")

    normalized_email = email.strip().lower()
    grant = get_beta_grant_by_email(normalized_email)
    if not grant:
        return None

    now = datetime.now(timezone.utc)
    current_expiry = _parse_iso(grant.get("expires_at")) or now
    new_expiry = max(current_expiry, now) + timedelta(days=days)

    try:
        response = (
            supabase.table(USER_TABLE)
            .update({"beta_expires_at": new_expiry.isoformat(), "beta_granted_by": granted_by.strip().lower()})
            .eq("email", normalized_email)
            .execute()
        )
    except Exception:
        return None
    if not response.data:
        return None
    return {**grant, "expires_at": new_expiry.isoformat(), "active": True}


def revoke_beta_by_email(email: str) -> dict[str, object] | None:
    """Clears the current grant entirely (all 4 columns back to null).
    Returns the grant state that WAS revoked (for the admin audit-log
    entry), or `None` if there was nothing to revoke. Never touches
    `vt_users.plan`/`premium`/Stripe data/any wellness data — the user
    simply falls back to their real underlying plan on their very next
    request, with nothing deleted."""
    normalized_email = email.strip().lower()
    previous = get_beta_grant_by_email(normalized_email)
    if not previous:
        return None

    try:
        supabase.table(USER_TABLE).update(
            {"beta_plan": None, "beta_started_at": None, "beta_expires_at": None, "beta_granted_by": None}
        ).eq("email", normalized_email).execute()
    except Exception:
        return None
    return previous


def has_feature(email: str, feature: str) -> bool:
    """The central entitlement check — routers should call this instead of
    a raw `if premium` / `if plan == ...`. Beta-aware: uses the EFFECTIVE
    plan (real paid plan OR an active temporary Beta grant, whichever
    ranks higher), never the real plan alone — this is what makes a Pro
    Beta grant actually unlock real Pro features (family.py, health.py,
    google_health.py, profile.py, daily_planning.py all call this)."""
    plan = get_effective_plan_by_email(email)
    return feature in FEATURE_SETS[plan]


def get_active_goal_limit(email: str) -> int | None:
    """Max number of simultaneously ACTIVE `vt_wellness_goals` rows for this
    user's plan, or `None` for unlimited (Pro/Family via `multiple_goals`).
    Used only to gate NEW activations — never removes/deactivates a goal a
    user already has."""
    if has_feature(email, "multiple_goals"):
        return None
    return FREE_TIER_MAX_ACTIVE_GOALS


def set_plan_by_email(email: str, plan: PlanId) -> bool:
    """The only place that should write `vt_users.plan`. Keeps the legacy
    `premium` boolean in sync via `routers/users.py::set_premium_by_email`
    (local import here avoids a circular import at module-load time, same
    established pattern as `automation_engine.py`'s local imports of
    sibling modules) — this ALSO means an intermediate `plan='premium'`
    write can happen first for a Free->Pro/Family change (the shared
    "never downgrade pro/family" guard in that function only ever upgrades
    to 'premium'), which the explicit `plan` write immediately below then
    corrects to the exact requested tier. Returns True only if a row was
    ACTUALLY updated (checks `response.data`, not just "no exception") —
    a Postgrest UPDATE matching zero rows does not raise, so relying on
    the absence of an exception alone would silently report success for a
    write that changed nothing."""
    if plan not in VALID_PLANS:
        raise ValueError(f"Unbekannter Tarif: {plan!r}")
    normalized_email = email.strip().lower()

    from ..routers.users import set_premium_by_email, sync_cached_plan  # local import: breaks core<->routers cycle

    set_premium_by_email(normalized_email, plan != "free")

    try:
        response = supabase.table(USER_TABLE).update({"plan": plan}).eq("email", normalized_email).execute()
        updated = bool(response.data)
    except Exception:
        return False

    if updated:
        sync_cached_plan(normalized_email, plan)
    return updated


def resolve_plan_from_price_id(price_id: str | None) -> PlanId | None:
    """Reverse lookup of `core/plans.py::PLAN_PRICE_ENV_MAP` — given a
    Stripe Price ID actually charged, returns which plan it corresponds to,
    or None if it doesn't match any currently configured price (e.g. an
    old/removed price). Used by the Stripe webhook to store the tier that
    was ACTUALLY purchased, instead of always assuming "premium"."""
    if not price_id:
        return None
    for plan, intervals in PLAN_PRICE_ENV_MAP.items():
        for env_name in intervals.values():
            if os.getenv(env_name, "").strip() == price_id:
                return plan  # type: ignore[return-value]

    # Legacy fallback: the original single-tier setup's `STRIPE_PRICE_ID`
    # env var always meant "premium, monthly" (see `plans.py`).
    legacy_env = os.getenv("STRIPE_PRICE_ID", "").strip()
    if legacy_env and price_id == legacy_env:
        return "premium"
    return None
