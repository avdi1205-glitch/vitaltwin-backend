from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.supabase import supabase

router = APIRouter()


class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class EmailRequest(BaseModel):
    email: str


@router.post("/register")
async def register(req: RegisterRequest):
    try:
        response = supabase.auth.sign_up(
            {
                "email": req.email,
                "password": req.password,
                "options": {"data": {"full_name": req.full_name}},
            }
        )
        return {
            "message": "Registrierung erfolgreich. Bitte E-Mail bestätigen.",
            "user_id": response.user.id,
        }
    except Exception as e:
        detail = str(e)
        status_code = 429 if "rate limit" in detail.lower() else 400
        raise HTTPException(status_code, detail)


@router.post("/resend-confirmation")
async def resend_confirmation(req: EmailRequest):
    try:
        supabase.auth.resend({"type": "signup", "email": req.email})
        return {"message": "Bestaetigungs-E-Mail wurde erneut gesendet."}
    except Exception as e:
        detail = str(e)
        status_code = 429 if "rate limit" in detail.lower() else 400
        raise HTTPException(status_code, detail)


@router.post("/login")
async def login(req: LoginRequest):
    try:
        response = supabase.auth.sign_in_with_password(
            {"email": req.email, "password": req.password}
        )
        return {
            "access_token": response.session.access_token,
            "user": response.user,
        }
    except Exception as e:
        detail = str(e)
        detail_lower = detail.lower()

        if "email not confirmed" in detail_lower:
            raise HTTPException(401, "Bitte bestaetige zuerst deine E-Mail-Adresse.")

        if "invalid login credentials" in detail_lower:
            raise HTTPException(401, "Falsche Zugangsdaten")

        raise HTTPException(401, detail)
