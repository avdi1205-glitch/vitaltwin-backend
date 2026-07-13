from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()

users_store: dict[str, dict[str, object]] = {}
token_to_email: dict[str, str] = {}

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str

class LoginRequest(BaseModel):
    email: str
    password: str


def get_email_by_token(token: str | None) -> str | None:
    if not token:
        return None
    return token_to_email.get(token)


def set_premium_by_email(email: str, premium: bool) -> bool:
    user = users_store.get(email.lower())
    if not user:
        return False
    user["premium"] = premium
    return True

@router.post("/register")
async def register(req: RegisterRequest):
    email = req.email.strip().lower()
    if email in users_store:
        raise HTTPException(status_code=400, detail="E-Mail ist bereits registriert")

    users_store[email] = {
        "password": req.password,
        "full_name": req.full_name,
        "premium": False,
    }

    return {"message": "Registrierung erfolgreich", "email": email}

@router.post("/login")
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    user = users_store.get(email)

    if not user or user.get("password") != req.password:
        raise HTTPException(status_code=401, detail="Ungueltige E-Mail oder Passwort")

    token = f"vt_{uuid4().hex}"
    token_to_email[token] = email

    return {
        "access_token": token,
        "message": "Login erfolgreich",
        "email": email,
        "premium": bool(user.get("premium", False)),
    }


@router.get("/me")
async def me(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    token = authorization.split(" ", 1)[1].strip()
    email = get_email_by_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Session abgelaufen")

    user = users_store.get(email)
    if not user:
        raise HTTPException(status_code=404, detail="User nicht gefunden")

    return {
        "email": email,
        "full_name": user.get("full_name"),
        "premium": bool(user.get("premium", False)),
    }