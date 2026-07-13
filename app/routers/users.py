from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core.supabase import supabase

router = APIRouter()

users_store: dict[str, dict[str, object]] = {}
token_to_email: dict[str, str] = {}
USER_TABLE = "vt_users"

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str

class LoginRequest(BaseModel):
    email: str
    password: str


def _db_get_user(email: str) -> dict[str, object] | None:
    try:
        response = (
            supabase.table(USER_TABLE)
            .select("email,full_name,password,premium")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        data = response.data or []
        if not data:
            return None
        return data[0]
    except Exception:
        return None


def _db_create_user(email: str, full_name: str, password: str) -> bool:
    try:
        (
            supabase.table(USER_TABLE)
            .insert(
                {
                    "email": email,
                    "full_name": full_name,
                    "password": password,
                    "premium": False,
                }
            )
            .execute()
        )
        return True
    except Exception:
        return False


def _db_update_premium(email: str, premium: bool) -> bool:
    try:
        (
            supabase.table(USER_TABLE)
            .update({"premium": premium})
            .eq("email", email)
            .execute()
        )
        return True
    except Exception:
        return False


def _normalize_user_record(record: dict[str, object]) -> dict[str, object]:
    return {
        "password": record.get("password", ""),
        "full_name": record.get("full_name", ""),
        "premium": bool(record.get("premium", False)),
    }


def _get_user(email: str) -> dict[str, object] | None:
    user = users_store.get(email)
    if user:
        return user

    db_user = _db_get_user(email)
    if db_user:
        normalized = _normalize_user_record(db_user)
        users_store[email] = normalized
        return normalized

    return None


def get_email_by_token(token: str | None) -> str | None:
    if not token:
        return None
    return token_to_email.get(token)


def set_premium_by_email(email: str, premium: bool) -> bool:
    normalized_email = email.lower()
    user = _get_user(normalized_email)

    if user:
        user["premium"] = premium

    db_updated = _db_update_premium(normalized_email, premium)
    return bool(user) or db_updated

@router.post("/register")
async def register(req: RegisterRequest):
    email = req.email.strip().lower()

    if _get_user(email):
        raise HTTPException(status_code=400, detail="E-Mail ist bereits registriert")

    created_in_db = _db_create_user(email, req.full_name, req.password)

    users_store[email] = {
        "password": req.password,
        "full_name": req.full_name,
        "premium": False,
    }

    if not created_in_db:
        # Keep demo fallback behavior if Supabase table is not available yet.
        users_store[email]["storage"] = "memory"

    return {"message": "Registrierung erfolgreich", "email": email}

@router.post("/login")
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    user = _get_user(email)

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

    user = _get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="User nicht gefunden")

    return {
        "email": email,
        "full_name": user.get("full_name"),
        "premium": bool(user.get("premium", False)),
    }