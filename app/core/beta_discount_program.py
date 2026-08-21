"""Automated "first 20 active beta testers" discount program: 50% off any
paid plan (Premium/Pro/Family) for 6 months, awarded once per person to
the first 20 users whose EARLIEST real in-app action (check-in, twin
calculation, or Health Connect sync) occurs on or after
`PROGRAM_LAUNCHED_AT` — never retroactively to users who were already
active before this feature existed (see `maybe_claim_discount_slot`'s
docstring for the exact non-retroactivity guarantee).

Race-safe slot allocation (migration 043): application code never counts
rows itself to decide whether a slot is free — a Postgres SEQUENCE
(`beta_discount_slot_seq`, capped 1..20) does that atomically, via the
`claim_beta_discount_slot` SQL function called here through `.rpc(...)`
(the first RPC/stored-function call in this codebase — see the
migration's own comment for why: supabase-py has no raw SQL connection,
so a true row lock isn't reachable from application code).

STRIPE TOUCH: creates one shared 50%/6-months Coupon (idempotent, created
once) plus a restricted, single-use Promotion Code per grantee. The
resulting `stripe_promotion_code_id` is attached to a NEW Checkout Session
at `create_checkout` time (repeatable — safe even across several abandoned
sessions) and only marked `status='applied'` once the Stripe webhook
confirms a REAL active/trialing subscription for that email (never at
session-creation time, so an abandoned checkout never burns the slot).

CANCEL + RESUBSCRIBE (verified against Stripe's own docs, 2026-08-21,
https://docs.stripe.com/billing/subscriptions/coupons#redemption-limits
+ #deactivate): each grantee's Promotion Code has `max_redemptions=1`.
Per Stripe's "Redemption limits" section, a customer's redemption of a
promotion code counts toward the coupon's redemption limit; per
"Deactivate promotion codes", once a code "reaches its maximum redemption
limit or its expiration date, it becomes permanently inactive... these
promotion codes can't be reactivated" — Stripe itself permanently blocks
reuse once genuinely redeemed (i.e. once attached to a COMPLETED
Checkout Session, not merely referenced on an abandoned/unpaid one — the
Coupons-vs-Promotion-Codes page frames "redeem" as the customer-facing
completion event, matching Checkout's own pending-until-completed
architecture). `status='applied'` in `vt_beta_discount_grants` +
`get_unused_promotion_code()`'s `status == 'granted'` gate is a SECOND,
application-level layer on top of this — defense in depth, not the sole
safeguard.

EXPIRATION: a grant not converted into a real checkout within
`EXPIRATION_MONTHS` (12) of being granted lazily transitions to
`status='expired'` the next time it's read (`_expire_if_past_due`, no
scheduler exists in this codebase — matches its established
compute/update-on-read convention). An expired slot's `slot_number` is
NEVER recycled/reissued (the Postgres sequence only ever increments).

COUPON STACKING: verified via grep (2026-08-21) that `SHARED_COUPON_ID`
is the ONLY `stripe.Coupon`/`stripe.PromotionCode` created anywhere in
this codebase — no second coupon system exists today, so no additional
stacking guard is needed. If a second, unrelated Stripe coupon system is
ever introduced, revisit whether `discounts` in `create_checkout` could
ever need more than one entry (Stripe supports up to 20 stacked
discounts) before assuming this single-entry design still holds.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import stripe
from dateutil.relativedelta import relativedelta

from .supabase import supabase

logger = logging.getLogger(__name__)

GRANTS_TABLE = "vt_beta_discount_grants"
TOTAL_DISCOUNT_SLOTS = 20
DISCOUNT_PERCENT = 50
DISCOUNT_DURATION_MONTHS = 6
EXPIRATION_MONTHS = 12
SHARED_COUPON_ID = "vitaltwin_beta_first20_50pct_6mo"

# Anything before this instant is pre-existing activity that must NEVER
# retroactively earn a slot — only genuinely new first-actions from here
# on are eligible. This is the actual go-live moment; update only if the
# founder decides to launch later than this value.
PROGRAM_LAUNCHED_AT = datetime(2026, 8, 21, 19, 0, 0, tzinfo=timezone.utc)

_CHECKIN_TABLE = "vt_daily_wellness_entries"
_TWIN_CALC_TABLE = "vt_twin_calculations"
_HEALTH_SYNC_TABLE = "health_sync_runs"


def _excluded_emails() -> set[str]:
    """Admin/founder/test accounts that must NEVER receive a discount
    slot. `BETA_DISCOUNT_EXCLUDED_EMAILS` env var, comma-separated,
    case-insensitive — deliberately NOT hardcoded so the list can change
    without a code deploy."""
    raw = os.getenv("BETA_DISCOUNT_EXCLUDED_EMAILS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _earliest_email_scoped_timestamp(table: str, column: str, email: str) -> datetime | None:
    """Earliest `column` value for this email in `table`, or None. Read-only."""
    try:
        response = (
            supabase.table(table)
            .select(column)
            .eq("email", email)
            .order(column)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        return _parse_timestamp(rows[0].get(column))
    except Exception:
        return None


def _earliest_completed_health_connect_sync(user_id: int) -> datetime | None:
    try:
        response = (
            supabase.table(_HEALTH_SYNC_TABLE)
            .select("started_at")
            .eq("user_id", user_id)
            .eq("provider", "health_connect")
            .eq("status", "completed")
            .order("started_at")
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        return _parse_timestamp(rows[0].get("started_at"))
    except Exception:
        return None


def detect_first_real_action(email: str, user_id: int | None) -> tuple[datetime, str] | None:
    """The user's EARLIEST-ever action across all 3 qualifying sources, or
    None if they have none yet. Does not itself decide program eligibility
    — see `maybe_claim_discount_slot`, which additionally checks the
    result against `PROGRAM_LAUNCHED_AT`."""
    candidates: list[tuple[datetime, str]] = []

    checkin_at = _earliest_email_scoped_timestamp(_CHECKIN_TABLE, "created_at", email)
    if checkin_at is not None:
        candidates.append((checkin_at, "checkin"))

    twin_calc_at = _earliest_email_scoped_timestamp(_TWIN_CALC_TABLE, "created_at", email)
    if twin_calc_at is not None:
        candidates.append((twin_calc_at, "twin_calculation"))

    if user_id is not None:
        sync_at = _earliest_completed_health_connect_sync(user_id)
        if sync_at is not None:
            candidates.append((sync_at, "health_connect_sync"))

    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])


def _ensure_shared_coupon_exists() -> str | None:
    """Idempotently creates the ONE shared Stripe Coupon all grantees'
    individual Promotion Codes point to. Returns its id, or None if Stripe
    isn't configured or the call fails (the grant itself still stands
    either way — see `maybe_claim_discount_slot`)."""
    if not stripe.api_key:
        return None
    try:
        stripe.Coupon.retrieve(SHARED_COUPON_ID)
        return SHARED_COUPON_ID
    except stripe.StripeError:
        pass  # expected on first-ever run: coupon doesn't exist yet
    try:
        stripe.Coupon.create(
            id=SHARED_COUPON_ID,
            percent_off=DISCOUNT_PERCENT,
            duration="repeating",
            duration_in_months=DISCOUNT_DURATION_MONTHS,
            name="VitalTwin Beta \u2013 erste 20 aktive Tester",
        )
        return SHARED_COUPON_ID
    except stripe.StripeError:
        logger.error("beta_discount_program: failed to create shared Stripe coupon %s", SHARED_COUPON_ID, exc_info=True)
        return None


def _create_promotion_code_for_grant(email: str, slot_number: int, expires_at: datetime) -> str | None:
    """One restricted, single-use Promotion Code per grantee
    (max_redemptions=1), for individual auditability. `expires_at` mirrors
    the grant's own 12-month window at the Stripe level too (defense in
    depth alongside the lazy DB-side expiry check). Returns its id, or
    None if Stripe isn't configured or the call fails."""
    coupon_id = _ensure_shared_coupon_exists()
    if not coupon_id:
        return None
    try:
        # stripe==15.3.0 / API 2026-06-24.dahlia rejects the legacy flat
        # `coupon=...` kwarg here (verified against real Stripe Test Mode,
        # 2026-08-21) — must be the nested `promotion` form.
        promo = stripe.PromotionCode.create(
            promotion={"type": "coupon", "coupon": coupon_id},
            max_redemptions=1,
            expires_at=int(expires_at.timestamp()),
            metadata={
                "vitaltwin_beta_discount_email": email,
                "vitaltwin_beta_discount_slot": str(slot_number),
            },
        )
        return promo.get("id") if isinstance(promo, dict) else getattr(promo, "id", None)
    except stripe.StripeError:
        logger.error("beta_discount_program: failed to create promotion code for %s (slot %s)", email, slot_number, exc_info=True)
        return None


def maybe_claim_discount_slot(email: str, user_id: int | None = None) -> dict[str, object] | None:
    """Call this right after a check-in save, a twin calculation, or a
    completed Health Connect sync succeeds. Never raises — a failure here
    must never break the request it's called from.

    Returns None if the user isn't eligible yet: no qualifying action at
    all, OR their earliest one predates `PROGRAM_LAUNCHED_AT`. This is the
    non-retroactivity guard — a user who was already active before this
    feature launched always has an earlier `detect_first_real_action`
    timestamp than the cutoff, so they can never trigger a grant no matter
    which of the 3 actions they perform next.

    Otherwise returns the DB claim result: {"slot_number": int|None,
    "granted": bool} — `granted=False` once all 20 slots are already
    taken (no error, no exception, just an honest "not this time").
    """
    try:
        if email.strip().lower() in _excluded_emails():
            return None
        first_action = detect_first_real_action(email, user_id)
        if first_action is None:
            return None
        occurred_at, source = first_action
        if occurred_at < PROGRAM_LAUNCHED_AT:
            return None

        expires_at = datetime.now(timezone.utc) + relativedelta(months=EXPIRATION_MONTHS)
        response = supabase.rpc(
            "claim_beta_discount_slot",
            {
                "p_email": email,
                "p_first_real_usage_at": occurred_at.isoformat(),
                "p_first_real_usage_source": source,
                "p_expires_at": expires_at.isoformat(),
            },
        ).execute()
        rows = response.data or []
        result = rows[0] if rows else None
        if result and result.get("granted") and result.get("slot_number") is not None:
            promo_id = _create_promotion_code_for_grant(email, result["slot_number"], expires_at)
            if promo_id:
                try:
                    supabase.table(GRANTS_TABLE).update(
                        {"stripe_coupon_id": SHARED_COUPON_ID, "stripe_promotion_code_id": promo_id}
                    ).eq("email", email).execute()
                except Exception:
                    pass
        return result
    except Exception:
        return None


def get_discount_grant_for_email(email: str) -> dict[str, object] | None:
    """Read-only lookup for the user's own account-transparency view —
    never exposes any OTHER user's row. Lazily expires a past-due grant
    (see `_expire_if_past_due`) before returning it."""
    try:
        response = supabase.table(GRANTS_TABLE).select("*").eq("email", email).limit(1).execute()
        rows = response.data or []
        if not rows:
            return None
        return _expire_if_past_due(rows[0])
    except Exception:
        return None


def _expire_if_past_due(grant: dict[str, object]) -> dict[str, object]:
    """Lazily transitions a 'granted' row to 'expired' the next time it's
    read, once `EXPIRATION_MONTHS` have passed with no checkout — no
    scheduler exists in this codebase (matches its established
    compute/update-on-read convention, e.g. the Founder Daily Briefing),
    so a read is the natural trigger point. Never touches 'applied'/
    'revoked' rows. The slot itself is never freed either way — the
    Postgres sequence only ever increments."""
    if grant.get("status") != "granted":
        return grant
    expires_at = _parse_timestamp(grant.get("expires_at"))
    if expires_at is None or datetime.now(timezone.utc) < expires_at:
        return grant
    try:
        supabase.table(GRANTS_TABLE).update({"status": "expired"}).eq("email", grant["email"]).eq("status", "granted").execute()
    except Exception:
        pass
    return {**grant, "status": "expired"}


def get_unused_promotion_code(email: str) -> str | None:
    """The Stripe Promotion Code id to attach to a NEW checkout session for
    this email, or None if they have no grant, it has no code yet (Stripe
    not configured at grant time), or it was already used on a real
    subscription. Safe to call repeatedly across multiple checkout
    attempts (including abandoned ones)."""
    grant = get_discount_grant_for_email(email)
    if not grant or grant.get("status") != "granted":
        return None
    code = grant.get("stripe_promotion_code_id")
    return code if isinstance(code, str) and code else None


def mark_grant_applied(email: str) -> None:
    """Flips a 'granted' row to 'applied' once Stripe confirms a real paid
    subscription for this email — never at checkout-session-creation time.
    Idempotent: a grant already 'applied' (or none at all) is left
    untouched, never re-applied or overwritten."""
    try:
        supabase.table(GRANTS_TABLE).update(
            {"status": "applied", "applied_at": datetime.now(timezone.utc).isoformat()}
        ).eq("email", email).eq("status", "granted").execute()
    except Exception:
        pass


def list_discount_grants() -> list[dict[str, object]]:
    """Admin-facing list of every grant, ordered by slot number."""
    try:
        response = supabase.table(GRANTS_TABLE).select("*").order("slot_number").execute()
        return response.data or []
    except Exception:
        return []
