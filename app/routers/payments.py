import os

import stripe
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class CreateCheckout(BaseModel):
    price_id: str | None = None


@router.post('/create-checkout')
async def create_checkout(data: CreateCheckout):
    stripe_secret_key = os.getenv('STRIPE_SECRET_KEY')
    price_id = data.price_id or os.getenv('STRIPE_PRICE_ID')
    payment_link = os.getenv('STRIPE_PAYMENT_LINK')

    if payment_link:
        return {'url': payment_link}

    if not stripe_secret_key:
        raise HTTPException(500, 'STRIPE_SECRET_KEY fehlt in der Backend-.env')

    if not price_id:
        raise HTTPException(500, 'STRIPE_PRICE_ID fehlt in der Backend-.env')

    stripe.api_key = stripe_secret_key

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url='http://localhost:3000/success',
            cancel_url='http://localhost:3000/preise',
        )
        return {'url': session.url}
    except Exception as e:
        raise HTTPException(400, str(e))