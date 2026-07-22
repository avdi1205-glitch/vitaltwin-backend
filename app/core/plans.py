"""Central plan / permission / Stripe price configuration for VitalTwin.

Single source of truth on the backend for which subscription plans exist,
which Stripe price IDs back them (via env vars only — never trust a raw
price_id sent from the client), and which feature permissions each plan
grants. Keep this in sync with `frontend/lib/plans.ts` (the two repos are
deployed separately, so the definitions are duplicated by necessity, not
imported directly).
"""

import os

PlanId = str  # "free" | "premium" | "pro" | "family"
BillingInterval = str  # "monthly" | "yearly"

# Maps plan -> interval -> the environment variable name holding the Stripe
# Price ID for that plan/interval combination. `free` has no Stripe price.
PLAN_PRICE_ENV_MAP: dict[str, dict[str, str]] = {
    "premium": {
        "monthly": "STRIPE_PRICE_PREMIUM_MONTHLY",
        "yearly": "STRIPE_PRICE_PREMIUM_YEARLY",
    },
    "pro": {
        "monthly": "STRIPE_PRICE_PRO_MONTHLY",
        "yearly": "STRIPE_PRICE_PRO_YEARLY",
    },
    "family": {
        "monthly": "STRIPE_PRICE_FAMILY_MONTHLY",
        "yearly": "STRIPE_PRICE_FAMILY_YEARLY",
    },
}

# Legacy fallback: the original single-tier setup only had STRIPE_PRICE_ID
# for what is now "premium" / "monthly". Keep honoring it so existing
# deployments (Railway) keep working without immediately renaming the var.
_LEGACY_PREMIUM_MONTHLY_ENV = "STRIPE_PRICE_ID"

# Central feature/permission definitions per plan. This is descriptive
# metadata (used for e.g. future entitlement checks); it does not yet gate
# any endpoint, since the database currently only stores a boolean
# `premium` flag per user (see open tasks in the Block 3 report).
PLAN_FEATURES: dict[str, dict[str, object]] = {
    "free": {
        "ai_questions_per_day": 3,
        "history_days": 7,
        "max_profiles": 1,
        "has_ads": True,
        "has_weekly_reports": False,
        "has_lifestyle_simulations": False,
        "has_family_features": False,
    },
    "premium": {
        "ai_questions_per_day": "fair-unlimited",
        "history_days": "extended",
        "max_profiles": 1,
        "has_ads": False,
        "has_weekly_reports": True,
        "has_lifestyle_simulations": False,
        "has_family_features": False,
    },
    "pro": {
        "ai_questions_per_day": "fair-unlimited",
        "history_days": "unlimited",
        "max_profiles": 1,
        "has_ads": False,
        "has_weekly_reports": True,
        "has_lifestyle_simulations": True,
        "has_family_features": False,
    },
    "family": {
        "ai_questions_per_day": "fair-unlimited",
        "history_days": "unlimited",
        "max_profiles": 6,
        "has_ads": False,
        "has_weekly_reports": True,
        "has_lifestyle_simulations": True,
        "has_family_features": True,
    },
}

# Concrete daily "Frag deinen Twin" chat message caps per plan (Block 6).
# PLAN_FEATURES above describes premium/pro/family as "fair-unlimited" for
# marketing purposes, but a real numeric ceiling is required server-side to
# keep AI provider costs bounded. Note: the database currently only stores a
# boolean `premium` flag (see Block 3 open items), so pro/family accounts are
# indistinguishable from premium today and get the same limit until a real
# `plan` field exists.
CHAT_DAILY_LIMITS: dict[str, int] = {
    "free": 3,
    "premium": 30,
    "pro": 60,
    "family": 30,
}


def get_chat_daily_limit(plan: str) -> int:
    return CHAT_DAILY_LIMITS.get(plan, CHAT_DAILY_LIMITS["free"])


# Twin Intelligence Core — Etappe 7 §7: server-side context-size ceiling per
# plan (character budget for the assembled Twin Context — see
# `services/twin_context.py`). FREE gets a small, essential-only context;
# PREMIUM/PRO get progressively more room for trends/patterns/memories
# ("erweiterter Kontext"/"mehr Langzeitkontext"). FAMILY is currently
# indistinguishable from PREMIUM in the database (see Block 3 open items) and
# gets the same budget — "getrennte private Profile pro Familienmitglied"
# already holds naturally because every query is scoped by the requesting
# user's own `email` (each family member has their own login), not by a
# shared family record.
CONTEXT_CHAR_LIMITS: dict[str, int] = {
    "free": 600,
    "premium": 1500,
    "pro": 2500,
    "family": 1500,
}


def get_context_char_limit(plan: str) -> int:
    return CONTEXT_CHAR_LIMITS.get(plan, CONTEXT_CHAR_LIMITS["free"])


def get_configured_price_id(plan: str, interval: str) -> str | None:
    """Returns the Stripe price ID configured for a plan/interval via env vars,
    or None if that plan/interval is not (yet) available for purchase."""
    if plan == "premium" and interval == "monthly":
        legacy = os.getenv(_LEGACY_PREMIUM_MONTHLY_ENV, "").strip()
        primary = os.getenv(PLAN_PRICE_ENV_MAP["premium"]["monthly"], "").strip()
        return primary or legacy or None

    env_name = PLAN_PRICE_ENV_MAP.get(plan, {}).get(interval)
    if not env_name:
        return None

    value = os.getenv(env_name, "").strip()
    return value or None


def get_all_configured_price_ids() -> set[str]:
    """Returns the set of every Stripe price ID currently configured across
    all plans/intervals via env vars. Used to validate that a price_id sent
    by the client actually corresponds to a real, intentionally configured
    VitalTwin plan — never trust a client-supplied price_id on its own."""
    configured: set[str] = set()
    for plan, intervals in PLAN_PRICE_ENV_MAP.items():
        for interval in intervals:
            price_id = get_configured_price_id(plan, interval)
            if price_id:
                configured.add(price_id)
    return configured
