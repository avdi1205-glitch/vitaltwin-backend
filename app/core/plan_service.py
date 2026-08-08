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
from typing import Literal

from .supabase import supabase
from .plans import PLAN_PRICE_ENV_MAP

PlanId = Literal["free", "premium", "pro", "family"]
VALID_PLANS: frozenset[str] = frozenset({"free", "premium", "pro", "family"})

USER_TABLE = "vt_users"

# ---------------------------------------------------------------------------
# Feature hierarchy — explicit supersets, not a numeric "rank" comparison,
# so adding a Pro- or Family-exclusive feature later can never accidentally
# remove something Premium already has.
# ---------------------------------------------------------------------------
_FREE_FEATURES: frozenset[str] = frozenset({
    "basic_dashboard",
    "basic_history",
})

_PREMIUM_FEATURES: frozenset[str] = _FREE_FEATURES | frozenset({
    "extended_history",  # "Erweiterter Verlauf" — GET /api/profile/trends 90d window
    "cgm_nutrition",  # CGM-Import + Ernährungstagebuch (routers/health.py)
})

# No Pro-exclusive feature is genuinely enforced server-side yet (see
# PLAN_ARCHITECTURE_REPORT.md) — Pro's set is currently identical to
# Premium's, which is the CORRECT, honest state: it structurally guarantees
# "Pro >= Premium" without inventing a feature that doesn't exist yet.
_PRO_FEATURES: frozenset[str] = _PREMIUM_FEATURES | frozenset()

# Same honesty rule for Family — no real multi-profile/family-sharing
# backend exists yet (see PLAN_ARCHITECTURE_REPORT.md), so Family's set is
# Premium's set, guaranteeing "Family >= Premium for general wellness
# features" without fabricating a family-only feature.
_FAMILY_FEATURES: frozenset[str] = _PREMIUM_FEATURES | frozenset()

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


def has_feature(email: str, feature: str) -> bool:
    """The central entitlement check — routers should call this instead of
    a raw `if premium` / `if plan == ...`."""
    plan = get_plan_by_email(email)
    return feature in FEATURE_SETS[plan]


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

    from ..routers.users import set_premium_by_email  # local import: breaks core<->routers cycle

    set_premium_by_email(normalized_email, plan != "free")

    try:
        response = supabase.table(USER_TABLE).update({"plan": plan}).eq("email", normalized_email).execute()
        return bool(response.data)
    except Exception:
        return False


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
