"""Stripe Billing — real subscriptions/payments/refunds (Founder OS
integration follow-up, 2026-08-01).

Populated exclusively by real Stripe webhook events, handled in
`routers/payments.py::stripe_webhook`:

- `customer.subscription.created`/`customer.subscription.updated` → upsert
  into `vt_stripe_subscriptions`
- `customer.subscription.deleted` → mark the row `canceled` + downgrade the
  user's `premium` flag to `False` (the subscription has genuinely ended —
  Stripe only fires this once the grace period from `cancel_at_period_end`
  is over, or on an immediate cancellation)
- `invoice.paid` → insert into `vt_stripe_payments` (the real revenue
  source — `amount_paid` is in the smallest currency unit, e.g. cents)
- `charge.refunded` → insert into `vt_stripe_refunds`

Every aggregation function here reads only these three tables — never
recomputes anything from a live Stripe API call (no added latency/rate-limit
exposure), and never fabricates a number when the tables are empty/
unreachable (`None`, not `0`, on failure)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .supabase import supabase

SUBSCRIPTION_TABLE = "vt_stripe_subscriptions"
PAYMENT_TABLE = "vt_stripe_payments"
REFUND_TABLE = "vt_stripe_refunds"

ACTIVE_SUBSCRIPTION_STATUSES = {"trialing", "active", "past_due"}


def upsert_subscription(
    *,
    email: str,
    stripe_subscription_id: str,
    status: str,
    stripe_customer_id: str | None = None,
    plan_price_id: str | None = None,
    current_period_end: str | None = None,
    cancel_at_period_end: bool = False,
    canceled_at: str | None = None,
) -> None:
    row = {
        "email": email,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "status": status,
        "plan_price_id": plan_price_id,
        "current_period_end": current_period_end,
        "cancel_at_period_end": cancel_at_period_end,
        "canceled_at": canceled_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table(SUBSCRIPTION_TABLE).upsert(row, on_conflict="stripe_subscription_id").execute()
    except Exception:
        pass


def record_payment(
    *,
    stripe_invoice_id: str,
    amount_paid: int,
    currency: str = "eur",
    email: str | None = None,
    stripe_customer_id: str | None = None,
    paid_at: str | None = None,
) -> None:
    row = {
        "email": email,
        "stripe_customer_id": stripe_customer_id,
        "stripe_invoice_id": stripe_invoice_id,
        "amount_paid": amount_paid,
        "currency": currency,
        "paid_at": paid_at or datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table(PAYMENT_TABLE).upsert(row, on_conflict="stripe_invoice_id").execute()
    except Exception:
        pass


def record_refund(
    *,
    stripe_refund_id: str,
    amount: int,
    currency: str = "eur",
    email: str | None = None,
    stripe_customer_id: str | None = None,
    stripe_charge_id: str | None = None,
    reason: str | None = None,
) -> None:
    row = {
        "email": email,
        "stripe_customer_id": stripe_customer_id,
        "stripe_charge_id": stripe_charge_id,
        "stripe_refund_id": stripe_refund_id,
        "amount": amount,
        "currency": currency,
        "reason": reason,
    }
    try:
        supabase.table(REFUND_TABLE).upsert(row, on_conflict="stripe_refund_id").execute()
    except Exception:
        pass


def _sum_payments_since(since_iso: str) -> int | None:
    try:
        rows = (
            supabase.table(PAYMENT_TABLE).select("amount_paid").gte("paid_at", since_iso).execute().data or []
        )
    except Exception:
        return None
    return sum(int(r.get("amount_paid") or 0) for r in rows)


def get_revenue_for_window(start_iso: str, end_iso: str | None = None) -> float | None:
    """Real revenue in EUR for an arbitrary [start, end) window — used for
    e.g. "gestern" where a plain `days=N` cutoff isn't precise enough."""
    try:
        query = supabase.table(PAYMENT_TABLE).select("amount_paid").gte("paid_at", start_iso)
        if end_iso:
            query = query.lt("paid_at", end_iso)
        rows = query.execute().data or []
    except Exception:
        return None
    return round(sum(int(r.get("amount_paid") or 0) for r in rows) / 100, 2)


def get_revenue_summary() -> dict:
    """Real revenue in EUR (converted from cents), computed purely from
    `vt_stripe_payments` — the exact set of `invoice.paid` events Stripe
    has sent us. `None` (not `0`) if the table can't be reached at all."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    revenue_today_cents = _sum_payments_since(today_start)
    revenue_month_cents = _sum_payments_since(month_start)

    unreachable = revenue_today_cents is None or revenue_month_cents is None
    return {
        "revenue_today": None if unreachable else round(revenue_today_cents / 100, 2),
        "revenue_month": None if unreachable else round(revenue_month_cents / 100, 2),
        "note": (
            "vt_stripe_payments nicht erreichbar oder Migration 023 noch nicht ausgeführt."
            if unreachable
            else "Berechnet aus echten invoice.paid-Webhook-Events (vt_stripe_payments) — kein fester Wert."
        ),
    }


def get_subscription_summary() -> dict:
    try:
        rows = supabase.table(SUBSCRIPTION_TABLE).select("status").execute().data or []
    except Exception:
        return {"active": None, "canceled": None, "note": "vt_stripe_subscriptions nicht erreichbar oder Migration 023 noch nicht ausgeführt."}

    active = sum(1 for r in rows if r.get("status") in ACTIVE_SUBSCRIPTION_STATUSES)
    canceled = sum(1 for r in rows if r.get("status") == "canceled")
    return {
        "active": active,
        "canceled": canceled,
        "note": "Berechnet aus echten customer.subscription.*-Webhook-Events (vt_stripe_subscriptions) — kein fester Wert.",
    }


def get_cancellations_since(since_iso: str) -> int | None:
    try:
        rows = (
            supabase.table(SUBSCRIPTION_TABLE)
            .select("canceled_at")
            .eq("status", "canceled")
            .gte("canceled_at", since_iso)
            .execute()
            .data
            or []
        )
    except Exception:
        return None
    return len(rows)


def get_refund_summary(*, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = supabase.table(REFUND_TABLE).select("amount").gte("created_at", since).execute().data or []
    except Exception:
        return {"count": None, "total": None, "note": "vt_stripe_refunds nicht erreichbar oder Migration 023 noch nicht ausgeführt."}

    total_cents = sum(int(r.get("amount") or 0) for r in rows)
    return {
        "count": len(rows),
        "total": round(total_cents / 100, 2),
        "note": "Berechnet aus echten charge.refunded-Webhook-Events (vt_stripe_refunds) — kein fester Wert.",
    }
