import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

import bcrypt
import jwt
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..core.supabase import supabase
from ..core.rate_limit import enforce_rate_limit
from ..core.plan_service import get_beta_grant_by_email, get_effective_plan_by_email, get_plan_by_email

router = APIRouter()

users_store: dict[str, dict[str, object]] = {}
feedback_store: list[dict[str, object]] = []
USER_TABLE = "vt_users"
FEEDBACK_TABLE = "vt_user_feedback"
CALC_TABLE = "vt_twin_calculations"

# Stateless session tokens (JWT): the token itself encodes and proves the
# identity, so login sessions survive backend restarts and work correctly
# across multiple backend instances (no shared in-memory token store needed).
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "").strip()
if not JWT_SECRET_KEY:
    # Insecure fallback so local/dev setups keep working without extra config.
    # Set JWT_SECRET_KEY in production so tokens stay valid across deploys.
    JWT_SECRET_KEY = "dev-insecure-jwt-secret-change-me"
    print("WARNING: JWT_SECRET_KEY is not set. Using an insecure development fallback.")


def _create_access_token(email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

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


class RequestPasswordResetRequest(BaseModel):
    email: str


class CompletePasswordResetRequest(BaseModel):
    access_token: str
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


def _ensure_supabase_auth_shadow_user(email: str) -> None:
    """Best-effort: make sure Supabase Auth knows this email so it can send reset emails.

    Our own login system stores credentials in the vt_users table, not Supabase Auth.
    Supabase can only send password-reset emails for accounts that exist in its own
    Auth store, so we lazily create a shadow account there. The random password set
    here is never used for anything: our real login always checks vt_users.
    """
    try:
        supabase.auth.sign_up({"email": email, "password": f"{uuid4().hex}Aa1!"})
    except Exception:
        # Already exists in Supabase Auth (expected for repeat requests) or sign-up disabled.
        pass


def _db_get_user(email: str) -> dict[str, object] | None:
    # Defensive two-step select: migration 027 (adds `vt_users.plan`) may
    # not have been run in Supabase yet when this code deploys (this repo's
    # established pattern — migrations are applied manually and often lag
    # behind a deploy by days). Selecting a column that does not exist yet
    # would make Postgrest reject the WHOLE query, which without this
    # fallback would make every existing user look logged-out/non-premium
    # until the migration runs — a real regression risk, not just
    # theoretical. Falls back to the pre-migration column set so existing
    # behavior is preserved either way; `plan` starts implicitly missing
    # (handled by `_normalize_user_record`'s legacy fallback) until the
    # migration is applied.
    try:
        response = (
            supabase.table(USER_TABLE)
            .select("email,full_name,password,premium,suspended,plan")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        data = response.data or []
        return data[0] if data else None
    except Exception:
        pass

    try:
        response = (
            supabase.table(USER_TABLE)
            .select("email,full_name,password,premium,suspended")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        data = response.data or []
        return data[0] if data else None
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


def _db_update_plan(email: str, plan: str) -> bool:
    try:
        (
            supabase.table(USER_TABLE)
            .update({"plan": plan})
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
    plan = str(record.get("plan") or "").strip().lower()
    if plan not in {"free", "premium", "pro", "family"}:
        # Defensive legacy fallback for rows that predate migration 027's
        # backfill (should not normally happen).
        plan = "premium" if bool(record.get("premium", False)) else "free"
    return {
        "password": record.get("password", ""),
        "full_name": record.get("full_name", ""),
        "premium": bool(record.get("premium", False)),
        "suspended": bool(record.get("suspended", False)),
        "plan": plan,
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
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    email = payload.get("sub")
    return str(email) if email else None


def _record_login_event(*, email: str, success: bool, request: Request) -> None:
    """Admin Control Center (Security Center / User Management 'Login
    Historie') — best-effort, fire-and-forget, mirrors
    `core/audit.py::record_audit_event`'s contract: a failed write here must
    never block an actual login attempt."""
    try:
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        supabase.table("vt_login_events").insert(
            {
                "email": email,
                "success": success,
                "ip_address": client_ip,
                "user_agent": user_agent,
            }
        ).execute()
    except Exception:
        pass


def set_premium_by_email(email: str, premium: bool) -> bool:
    """Legacy boolean setter — kept for every existing caller (admin
    premium-toggle, Stripe checkout-completed, activate-beta). Now also
    plan-aware to keep `vt_users.plan` truthful without requiring every
    caller to be rewritten (migration 027 / plan-architecture task):

    - `premium=False` always downgrades to `plan='free'` (a real
      cancellation/suspension must never leave a stale higher tier).
    - `premium=True` only ever sets `plan='premium'` when the account is
      currently `free` — it deliberately never downgrades an existing
      `pro`/`family` plan back to plain `premium` (e.g. Beta-Zugang must
      never demote a real paying Pro/Family customer). Callers that need to
      set an EXACT plan (e.g. the Stripe webhook resolving which tier was
      actually purchased) must use `core/plan_service.py::set_plan_by_email`
      instead.
    """
    normalized_email = email.lower()
    user = _get_user(normalized_email)

    if user:
        user["premium"] = premium
        current_plan = str(user.get("plan", "free") or "free")
        if not premium:
            user["plan"] = "free"
        elif current_plan not in {"pro", "family"}:
            user["plan"] = "premium"

    db_updated = _db_update_premium(normalized_email, premium)

    if not premium:
        _db_update_plan(normalized_email, "free")
    else:
        existing = _db_get_user(normalized_email)
        existing_plan = str((existing or {}).get("plan") or "free")
        if existing_plan not in {"pro", "family"}:
            _db_update_plan(normalized_email, "premium")

    return bool(user) or db_updated


def sync_cached_plan(email: str, plan: str) -> None:
    """`core/plan_service.py::set_plan_by_email` (admin Tarif-Wechsel,
    Stripe-Webhook) writes `vt_users.plan` directly in the DB and never
    touches the in-process `users_store` cache — without this, `_get_user`
    keeps serving the OLD cached plan (via `/api/users/me`, and therefore
    the dashboard) until this process restarts. Called right after that DB
    write succeeds to keep the cache truthful."""
    normalized_email = email.strip().lower()
    user = users_store.get(normalized_email)
    if user:
        user["plan"] = plan
        user["premium"] = plan != "free"


def invalidate_cached_user(email: str) -> None:
    """Evicts `email` from the in-process `users_store` cache — must be
    called whenever an account is actually deleted (GDPR self-service,
    admin hard-delete, QA cleanup — all go through
    `core/account_deletion.py::purge_all_user_data`). Without this, a
    deleted account could still log in successfully: `_get_user`'s
    cache-hit path never re-checks the DB, so a stale cached password hash
    would keep authenticating a user whose `vt_users` row no longer
    exists — a real bug found live (a "deleted" QA test account still
    logged in successfully after the row was gone from the database)."""
    users_store.pop(email.strip().lower(), None)


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
async def register(req: RegisterRequest, request: Request):
    enforce_rate_limit(request, "register", max_requests=5, window_seconds=60)
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
        "plan": "free",
    }

    if not created_in_db:
        # Keep demo fallback behavior if Supabase table is not available yet.
        users_store[email]["storage"] = "memory"

    # Best-effort: enables the forgot-password email flow via Supabase Auth later on.
    _ensure_supabase_auth_shadow_user(email)

    return {"message": "Registrierung erfolgreich", "email": email}

@router.post("/login")
async def login(req: LoginRequest, request: Request):
    enforce_rate_limit(request, "login", max_requests=10, window_seconds=60)
    email = req.email.strip().lower()
    user = _get_user(email)

    stored_password = str(user.get("password", "")) if user else ""
    if not user or not _verify_password(req.password, stored_password):
        _record_login_event(email=email, success=False, request=request)
        raise HTTPException(status_code=401, detail="Ungueltige E-Mail oder Passwort")

    if bool(user.get("suspended", False)):
        # Admin Control Center: a suspended account must not be able to log
        # in at all — otherwise "Nutzer sperren" would be purely decorative.
        _record_login_event(email=email, success=False, request=request)
        raise HTTPException(status_code=403, detail="Dieses Konto wurde gesperrt. Bitte kontaktiere den Support.")

    if not _is_hashed_password(stored_password):
        # Transparently migrate legacy plaintext passwords to a bcrypt hash on next login.
        migrated_hash = _hash_password(req.password)
        user["password"] = migrated_hash
        _db_update_password(email, migrated_hash)

    token = _create_access_token(email)
    _record_login_event(email=email, success=True, request=request)

    # Real, uncached, Beta-aware read (see `core/plan_service.py::
    # get_effective_plan_by_email`) — `user` here can be a stale in-process
    # cache entry (`_get_user`'s `users_store`) that no longer matches the
    # DB after an out-of-process plan change (admin Tarif-Wechsel, Stripe
    # webhook, Beta grant/revoke); the login response must never show a
    # premium/plan value that disagrees with what `/api/chat/status`/
    # `/api/profile/trends` actually enforce.
    plan = get_effective_plan_by_email(email)
    return {
        "access_token": token,
        "message": "Login erfolgreich",
        "email": email,
        "premium": plan != "free",
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

    token = _create_access_token(email)

    # Real, uncached, Beta-aware read — see the same comment in `/login` above.
    plan = get_effective_plan_by_email(email)
    return {
        "access_token": token,
        "message": "Login erfolgreich",
        "email": email,
        "premium": plan != "free",
        "plan": plan,
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

    # Real, uncached read (see `core/plan_service.py::get_plan_by_email`) —
    # `user` (from `_get_user`'s in-process cache) can silently disagree
    # with the DB after an out-of-process plan change, exactly the source
    # of a real bug found live: the dashboard's Plan badge showed "Pro"
    # here while `/api/chat/status`/`/api/profile/trends` (both already
    # uncached) correctly enforced "free" — `/me` must never be the one
    # stale source of truth.
    #
    # Beta Tester Program: `plan` is the EFFECTIVE plan (real plan OR an
    # active Beta grant, whichever ranks higher) so every existing
    # client-side `profile.plan === 'pro'`/`'family'` check already
    # sprinkled across the frontend keeps working correctly for Beta
    # testers with zero extra per-page changes. `real_plan` + `beta` expose
    # the underlying truth separately, purely for the "Pro · Beta-Tester"
    # label — never used for entitlement decisions on their own.
    real_plan = get_plan_by_email(email)
    beta_grant = get_beta_grant_by_email(email)
    plan = get_effective_plan_by_email(email)
    premium = plan != "free"
    starter_calc_remaining: int | None = None
    if not premium:
        starter_calc_remaining = 0 if _db_has_calculation(email) else 1

    return {
        "email": email,
        "full_name": user.get("full_name"),
        "premium": premium,
        "plan": plan,
        "real_plan": real_plan,
        "beta": (
            {
                "plan": beta_grant["plan"],
                "expires_at": beta_grant["expires_at"],
                "remaining_days": beta_grant["remaining_days"],
            }
            if beta_grant and beta_grant["active"]
            else None
        ),
        "starter_calc_remaining": starter_calc_remaining,
    }


# DEPRECATED: no current frontend call site (grep-verified) — confirm no other caller exists before removing.
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

    current_plan = str(user.get("plan") or ("premium" if user.get("premium") else "free"))
    if current_plan != "free":
        # Never downgrade a real paying Pro/Family (or already-Premium)
        # account back to a plain Beta-Premium state.
        return {"message": "Beta-Zugang ist bereits aktiv.", "premium": True, "plan": current_plan}

    updated = set_premium_by_email(email, True)
    if not updated:
        raise HTTPException(status_code=500, detail="Beta-Zugang konnte nicht aktiviert werden")

    return {
        "message": "Beta-Zugang kostenlos aktiviert. Danke, dass du als Tester dabei bist.",
        "premium": True,
        "plan": "premium",
    }


@router.post("/request-password-reset")
async def request_password_reset(req: RequestPasswordResetRequest):
    email = req.email.strip().lower()
    generic_response = {
        "message": "Falls ein Konto mit dieser E-Mail existiert, haben wir eine E-Mail zum Zurücksetzen des Passworts gesendet.",
    }

    if not _get_user(email):
        # Do not reveal whether the account exists.
        return generic_response

    frontend_base_url = os.getenv("FRONTEND_BASE_URL", "https://www.vitaltwin.de").rstrip("/")
    redirect_to = f"{frontend_base_url}/passwort-bestaetigen"

    _ensure_supabase_auth_shadow_user(email)
    try:
        supabase.auth.reset_password_for_email(email, {"redirect_to": redirect_to})
    except Exception:
        pass

    return generic_response


@router.post("/complete-password-reset")
async def complete_password_reset(req: CompletePasswordResetRequest):
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Neues Passwort muss mindestens 8 Zeichen haben")

    try:
        user_response = supabase.auth.get_user(req.access_token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Reset-Link ist ungültig oder abgelaufen") from exc

    supabase_user = getattr(user_response, "user", None)
    email = str(getattr(supabase_user, "email", "") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Reset-Link ist ungültig oder abgelaufen")

    new_hash = _hash_password(req.new_password)

    if not _get_user(email):
        full_name = email.split("@", 1)[0]
        _db_create_user(email, full_name, new_hash)
        users_store[email] = {"password": new_hash, "full_name": full_name, "premium": False}
    else:
        updated = set_password_by_email(email, new_hash)
        if not updated:
            raise HTTPException(status_code=500, detail="Passwort konnte nicht aktualisiert werden")

    return {"message": "Passwort erfolgreich aktualisiert. Du kannst dich jetzt anmelden.", "email": email}


@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest, request: Request, authorization: str | None = Header(default=None)):
    enforce_rate_limit(request, "feedback", max_requests=10, window_seconds=60)
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