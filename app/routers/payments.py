from fastapi import APIRouter, HTTPException
import stripe
from pydantic import BaseModel
from dotenv import load_dotenv
import os

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

router = APIRouter()

class CreateCheckout(BaseModel):
    price_id: str

@router.post("/create-checkout")
async def create_checkout(data: CreateCheckout):
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price': data.price_id, 'quantity': 1}],
            mode='subscription',
            success_url='http://localhost:3000/success',
            cancel_url='http://localhost:3000/preise',
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(400, str(e))