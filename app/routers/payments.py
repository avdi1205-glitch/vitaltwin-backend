from fastapi import APIRouter, Header, HTTPException, Request
import stripe
from pydantic import BaseModel
from dotenv import load_dotenv
import os

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

    if event.get("type") == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        metadata = session.get("metadata") or {}
        email = metadata.get("user_email") or session.get("customer_email") or session.get("client_reference_id")
        if isinstance(email, str) and email.strip():
            set_premium_by_email(email.strip().lower(), True)

    return {"received": True}