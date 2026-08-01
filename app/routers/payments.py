from fastapi import APIRouter, Header, HTTPException, Request
import stripe
from pydantic import BaseModel
from dotenv import load_dotenv
import os
from datetime import datetime, timezone

from ..core import stripe_billing
from ..core.plans import get_all_configured_price_ids, get_configured_price_id
from .users import get_email_by_token, set_premium_by_email

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

router = APIRouter()


def _trial_days() -> int:
    raw = os.getenv("STRIPE_TRIAL_DAYS", "30").strip()
    try:
        value = int(raw)
    except ValueError:
        return 30
    return max(0, value)

class CreateCheckout(BaseModel):
    price_id: str
    token: str | None = None


class CreatePlanCheckout(BaseModel):
    plan: str
    interval: str = "monthly"
    token: str | None = None


@router.post("/create-plan-checkout")
async def create_plan_checkout(data: CreatePlanCheckout):
    """Preferred entrypoint: the client only ever names a plan + interval
    (e.g. "pro" / "yearly"), never a raw Stripe price_id. The actual price_id
    is looked up server-side from env vars, so nothing the client sends can
    make us charge an unintended price."""
    price_id = get_configured_price_id(data.plan, data.interval)
    if not price_id:
        raise HTTPException(
            status_code=404,
            detail="Dieser Tarif ist noch nicht verfügbar. Trag dich gerne für die Warteliste ein.",
        )

    return await create_checkout(CreateCheckout(price_id=price_id, token=data.token))


@router.post("/create-checkout")
async def create_checkout(data: CreateCheckout):
    if not stripe.api_key:
        raise HTTPException(status_code=400, detail="Stripe Secret Key fehlt")

    if not data.price_id.startswith("price_"):
        raise HTTPException(status_code=400, detail="Ungueltige Preis-ID. Erwartet wird eine price_... ID")

    # Never trust a client-supplied price_id on its own: it must match one of
    # the prices we ourselves configured server-side via env vars.
    if data.price_id not in get_all_configured_price_ids():
        raise HTTPException(status_code=400, detail="Unbekannte oder nicht konfigurierte Preis-ID")

    email = get_email_by_token(data.token)
    if not email:
        raise HTTPException(status_code=401, detail="Bitte zuerst einloggen")

    frontend_base_url = os.getenv("FRONTEND_BASE_URL", "https://www.vitaltwin.de").rstrip("/")
    trial_days = _trial_days()

    checkout_payload = {
        "payment_method_types": ['card'],
        "line_items": [{'price': data.price_id, 'quantity': 1}],
        "mode": 'subscription',
        "customer_email": email,
        "client_reference_id": email,
        "metadata": {
            'user_email': email,
        },
        "success_url": f'{frontend_base_url}/dashboard?payment=success',
        "cancel_url": f'{frontend_base_url}/preise?payment=cancelled',
    }
    if trial_days > 0:
        checkout_payload["subscription_data"] = {"trial_period_days": trial_days}

    try:
        session = stripe.checkout.Session.create(**checkout_payload)
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(400, str(e))


def _unix_to_iso(timestamp: int | None) -> str | None:
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _resolve_customer_email(customer_id: str | None) -> str | None:
    """Real lookup via the Stripe API — never guessed. Subscription/charge
    objects don't carry the customer's email directly, only their Stripe
    customer id, so this is one extra (cheap, infrequent) live Stripe call
    per webhook that needs it. Never raises — a failed lookup just means
    the resulting row is stored with `email=None` instead of blocking the
    whole webhook."""
    if not customer_id:
        return None
    try:
        customer = stripe.Customer.retrieve(customer_id)
        email = customer.get("email") if isinstance(customer, dict) else getattr(customer, "email", None)
        return email.strip().lower() if isinstance(email, str) and email.strip() else None
    except Exception:
        return None


def _handle_checkout_completed(session: dict) -> None:
    metadata = session.get("metadata") or {}
    email = metadata.get("user_email") or session.get("customer_email") or session.get("client_reference_id")
    if isinstance(email, str) and email.strip():
        set_premium_by_email(email.strip().lower(), True)


def _handle_subscription_upsert(subscription: dict) -> None:
    email = _resolve_customer_email(subscription.get("customer"))
    items = (subscription.get("items") or {}).get("data") or []
    price_id = None
    if items and isinstance(items[0], dict):
        price = items[0].get("price") or {}
        price_id = price.get("id") if isinstance(price, dict) else None

    stripe_billing.upsert_subscription(
        email=email or "",
        stripe_subscription_id=subscription.get("id", ""),
        status=subscription.get("status", "unknown"),
        stripe_customer_id=subscription.get("customer"),
        plan_price_id=price_id,
        current_period_end=_unix_to_iso(subscription.get("current_period_end")),
        cancel_at_period_end=bool(subscription.get("cancel_at_period_end")),
    )


def _handle_subscription_deleted(subscription: dict) -> None:
    email = _resolve_customer_email(subscription.get("customer"))
    stripe_billing.upsert_subscription(
        email=email or "",
        stripe_subscription_id=subscription.get("id", ""),
        status="canceled",
        stripe_customer_id=subscription.get("customer"),
        canceled_at=datetime.now(timezone.utc).isoformat(),
    )
    # The subscription has genuinely ended (Stripe only fires this event
    # after any cancel_at_period_end grace period, or on immediate
    # cancellation) — downgrading here keeps `premium` truthful instead of
    # leaving it stuck `True` forever after a real cancellation.
    if email:
        set_premium_by_email(email, False)


def _handle_invoice_paid(invoice: dict) -> None:
    email = invoice.get("customer_email") or _resolve_customer_email(invoice.get("customer"))
    paid_at = _unix_to_iso((invoice.get("status_transitions") or {}).get("paid_at"))
    stripe_billing.record_payment(
        stripe_invoice_id=invoice.get("id", ""),
        amount_paid=int(invoice.get("amount_paid") or 0),
        currency=invoice.get("currency", "eur"),
        email=email,
        stripe_customer_id=invoice.get("customer"),
        paid_at=paid_at,
    )


def _handle_charge_refunded(charge: dict) -> None:
    email = (charge.get("billing_details") or {}).get("email") or _resolve_customer_email(charge.get("customer"))
    refunds = (charge.get("refunds") or {}).get("data") or []
    for refund in refunds:
        stripe_billing.record_refund(
            stripe_refund_id=refund.get("id", ""),
            amount=int(refund.get("amount") or 0),
            currency=refund.get("currency", "eur"),
            email=email,
            stripe_customer_id=charge.get("customer"),
            stripe_charge_id=charge.get("id"),
            reason=refund.get("reason"),
        )


_EVENT_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "customer.subscription.created": _handle_subscription_upsert,
    "customer.subscription.updated": _handle_subscription_upsert,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.paid": _handle_invoice_paid,
    "charge.refunded": _handle_charge_refunded,
}


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")):
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=400, detail="Stripe Webhook Secret fehlt")

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, webhook_secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Ungültige Stripe Signatur")

    handler = _EVENT_HANDLERS.get(event.get("type"))
    if handler is not None:
        handler(event.get("data", {}).get("object", {}))

    return {"received": True}