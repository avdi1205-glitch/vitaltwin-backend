import json
import os
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

import bcrypt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core.supabase import supabase

router = APIRouter()

users_store: dict[str, dict[str, object]] = {}
token_to_email: dict[str, str] = {}
feedback_store: list[dict[str, object]] = []
USER_TABLE = "vt_users"
FEEDBACK_TABLE = "vt_user_feedback"
CALC_TABLE = "vt_twin_calculations"

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str

class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class GoogleLoginRequest(BaseModel):
    credential: str


class FeedbackRequest(BaseModel):
    score: int
    message: str
    source: str | None = None


_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _is_hashed_password(value: str) -> bool:
    return value.startswith(_BCRYPT_PREFIXES)


def _verify_password(plain_password: str, stored_password: str) -> bool:
    if not stored_password:
        return False
    if _is_hashed_password(stored_password):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), stored_password.encode("utf-8"))
        except ValueError:
            return False
    # Legacy fallback for accounts created before password hashing was introduced.
    return plain_password == stored_password


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


def _db_update_password(email: str, password: str) -> bool:
    try:
        (
            supabase.table(USER_TABLE)
            .update({"password": password})
            .eq("email", email)
            .execute()
        )
        return True
    except Exception:
        return False


def _db_store_feedback(email: str, score: int, message: str, source: str | None) -> bool:
    try:
        (
            supabase.table(FEEDBACK_TABLE)
            .insert(
                {
                    "email": email,
                    "score": score,
                    "message": message,
                    "source": source or "dashboard",
                }
            )
            .execute()
        )
        return True
    except Exception:
        return False


def _db_has_calculation(email: str) -> bool:
    try:
        response = (
            supabase.table(CALC_TABLE)
            .select("id")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return bool(response.data)
    except Exception:
        return False


def _verify_google_credential(credential: str) -> dict[str, object]:
    url = f"https://oauth2.googleapis.com/tokeninfo?{urlencode({'id_token': credential})}"
    try:
        with urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise HTTPException(status_code=401, detail="Google-Login aktuell nicht verfügbar") from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Ungültiges Google-Token") from exc

    email = str(payload.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Google-Konto enthält keine E-Mail")

    if str(payload.get("email_verified", "")).lower() not in {"true", "1"}:
        raise HTTPException(status_code=401, detail="Google-E-Mail ist nicht verifiziert")

    expected_audience = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    if not expected_audience:
        raise HTTPException(status_code=500, detail="Google-Login ist serverseitig nicht konfiguriert")

    audience = str(payload.get("aud", "")).strip()
    if audience != expected_audience:
        raise HTTPException(status_code=401, detail="Google-Token ist für eine andere App ausgestellt")

    full_name = str(payload.get("name", "")).strip()
    if not full_name:
        full_name = email.split("@", 1)[0]

    return {"email": email, "full_name": full_name}


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


def set_password_by_email(email: str, password: str) -> bool:
    normalized_email = email.lower()
    user = _get_user(normalized_email)

    if user:
        user["password"] = password

    db_updated = _db_update_password(normalized_email, password)
    return bool(user) or db_updated


def is_premium_by_email(email: str) -> bool:
    normalized_email = email.strip().lower()
    user = _get_user(normalized_email)
    if not user:
        return False
    return bool(user.get("premium", False))

@router.post("/register")
async def register(req: RegisterRequest):
    email = req.email.strip().lower()

    if _get_user(email):
        raise HTTPException(status_code=400, detail="E-Mail ist bereits registriert")

    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Passwort muss mindestens 8 Zeichen haben")

    hashed_password = _hash_password(req.password)
    created_in_db = _db_create_user(email, req.full_name, hashed_password)

    users_store[email] = {
        "password": hashed_password,
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

    stored_password = str(user.get("password", "")) if user else ""
    if not user or not _verify_password(req.password, stored_password):
        raise HTTPException(status_code=401, detail="Ungueltige E-Mail oder Passwort")

    if not _is_hashed_password(stored_password):
        # Transparently migrate legacy plaintext passwords to a bcrypt hash on next login.
        migrated_hash = _hash_password(req.password)
        user["password"] = migrated_hash
        _db_update_password(email, migrated_hash)

    token = f"vt_{uuid4().hex}"
    token_to_email[token] = email

    return {
        "access_token": token,
        "message": "Login erfolgreich",
        "email": email,
        "premium": bool(user.get("premium", False)),
    }


@router.post("/google-login")
async def google_login(req: GoogleLoginRequest):
    verified = _verify_google_credential(req.credential)
    email = str(verified["email"])
    full_name = str(verified["full_name"])

    user = _get_user(email)
    if not user:
        # Random unguessable sentinel: Google-linked accounts never log in via password form.
        unusable_password_hash = _hash_password(uuid4().hex)
        created_in_db = _db_create_user(email, full_name, unusable_password_hash)
        users_store[email] = {
            "password": unusable_password_hash,
            "full_name": full_name,
            "premium": False,
        }
        if not created_in_db:
            users_store[email]["storage"] = "memory"
        user = users_store[email]

    token = f"vt_{uuid4().hex}"
    token_to_email[token] = email

    return {
        "access_token": token,
        "message": "Login erfolgreich",
        "email": email,
        "premium": bool(user.get("premium", False)),
    }


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    token = authorization.split(" ", 1)[1].strip()
    email = get_email_by_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Session abgelaufen")

    user = _get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="User nicht gefunden")

    stored_password = str(user.get("password", ""))
    if not _verify_password(req.current_password, stored_password):
        raise HTTPException(status_code=401, detail="Aktuelles Passwort ist falsch")

    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Neues Passwort muss mindestens 8 Zeichen haben")

    new_hash = _hash_password(req.new_password)
    updated = set_password_by_email(email, new_hash)
    if not updated:
        raise HTTPException(status_code=500, detail="Passwort konnte nicht aktualisiert werden")

    return {"message": "Passwort erfolgreich aktualisiert", "email": email}


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

    premium = bool(user.get("premium", False))
    starter_calc_remaining: int | None = None
    if not premium:
        starter_calc_remaining = 0 if _db_has_calculation(email) else 1

    return {
        "email": email,
        "full_name": user.get("full_name"),
        "premium": premium,
        "starter_calc_remaining": starter_calc_remaining,
    }


@router.post("/activate-beta")
async def activate_beta(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    token = authorization.split(" ", 1)[1].strip()
    email = get_email_by_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Session abgelaufen")

    user = _get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="User nicht gefunden")

    if bool(user.get("premium", False)):
        return {"message": "Beta-Zugang ist bereits aktiv.", "premium": True}

    updated = set_premium_by_email(email, True)
    if not updated:
        raise HTTPException(status_code=500, detail="Beta-Zugang konnte nicht aktiviert werden")

    return {
        "message": "Beta-Zugang kostenlos aktiviert. Danke, dass du als Tester dabei bist.",
        "premium": True,
    }


@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    token = authorization.split(" ", 1)[1].strip()
    email = get_email_by_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Session abgelaufen")

    score = max(1, min(5, req.score))
    message = req.message.strip()
    if len(message) < 5:
        raise HTTPException(status_code=400, detail="Bitte gib mindestens 5 Zeichen Feedback ein")

    saved_to_db = _db_store_feedback(email, score, message, req.source)
    feedback_store.append(
        {
            "email": email,
            "score": score,
            "message": message,
            "source": req.source or "dashboard",
        }
    )

    return {
        "message": "Danke für dein Feedback!",
        "saved": True,
        "storage": "database" if saved_to_db else "memory",
    }