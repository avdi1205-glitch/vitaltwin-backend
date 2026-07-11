import os

import stripe
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class CreateCheckout(BaseModel):
    price_id: str | None = None


def _looks_like_payment_link(value: str | None) -> bool:
    return bool(value and value.startswith('https://buy.stripe.com/'))


def _looks_like_secret_key(value: str | None) -> bool:
    return bool(value and value.startswith(('sk_test_', 'sk_live_')))


def _looks_like_price_id(value: str | None) -> bool:
    return bool(value and value.startswith('price_'))


@router.post('/create-checkout')
async def create_checkout(data: CreateCheckout):
    stripe_secret_key = os.getenv('STRIPE_SECRET_KEY')
    price_id = data.price_id or os.getenv('STRIPE_PRICE_ID')
    payment_link = os.getenv('STRIPE_PAYMENT_LINK')
    frontend_base_url = os.getenv('FRONTEND_BASE_URL', 'https://vitaltwin.de').rstrip('/')

    if _looks_like_payment_link(payment_link):
        return {'url': payment_link}

    if payment_link:
        raise HTTPException(500, 'STRIPE_PAYMENT_LINK ist gesetzt, aber ungueltig formatiert.')

    if not _looks_like_secret_key(stripe_secret_key):
        raise HTTPException(500, 'STRIPE_SECRET_KEY fehlt oder hat ein ungueltiges Format.')

    if not _looks_like_price_id(price_id):
        raise HTTPException(500, 'STRIPE_PRICE_ID fehlt oder hat ein ungueltiges Format.')

    stripe.api_key = stripe_secret_key

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=f'{frontend_base_url}/success',
            cancel_url=f'{frontend_base_url}/preise',
        )
        return {'url': session.url}
    except Exception as e:
        raise HTTPException(400, str(e))