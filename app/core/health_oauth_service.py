"""Google Health OAuth service — Authorization Code flow configuration,
DB-backed single-use CSRF state, and token exchange/revoke.

**Deliberately separate from `routers/users.py`'s Google Sign-In flow** — a
different OAuth client (its own Client ID/Secret in Google Cloud Console),
different scopes (`googlehealth.*` data scopes, not just identity/email),
and a different purpose (Sign-In only proves who the user is; this requests
offline access to actual health data). Nothing in `routers/users.py` or its
`_verify_google_credential` tokeninfo-based flow is imported, read, or
modified by this module.

Endpoints verified against official Google documentation (2026-08-02,
https://developers.google.com/health) before implementation — the Google
Health API is the documented, generally-available successor to the Fitbit
Web API, using Google's standard OAuth 2.0 Authorization Code flow. Defaults
below match the verified docs; every one is overridable via env var so a
future Google-side endpoint change doesn't require a code change.

**OAuth state — why DB-backed and not stateless JWT:** a stateless
signed-JWT state (this project's V1 draft) proves the state wasn't
*tampered with*, but does not prevent *replay* (the same valid state used
twice) and carries no server-side record of "this state was issued and is
still pending". `health_oauth_states` stores a SHA-256 hash of a
cryptographically random opaque token, checks expiry, and marks `used_at` on
first successful consumption — a second attempt to consume the same state
is rejected even though the value would still verify.

**PKCE:** deliberately NOT used. PKCE (RFC 7636) exists to protect *public*
clients (mobile/SPA apps that can't hold a client secret) from authorization
code interception. This is a confidential, server-side OAuth client — the
`client_secret` is stored only in Railway env vars and the code exchange
happens server-to-server — so PKCE adds no additional protection here. This
is a deliberate, documented decision, not an oversight.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from .health_errors import (
    HEALTH_NOT_CONFIGURED,
    HEALTH_OAUTH_STATE_EXPIRED,
    HEALTH_OAUTH_STATE_INVALID,
    HEALTH_OAUTH_STATE_USED,
    HEALTH_TOKEN_EXCHANGE_FAILED,
    HealthIntegrationError,
)
from .supabase import supabase

OAUTH_STATE_TABLE = "health_oauth_states"

PROVIDER = "google_health"

DEFAULT_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DEFAULT_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
DEFAULT_API_BASE = "https://health.googleapis.com/v4"

# Only request the scopes this integration actually uses, in the spec's
# priority order (activity/fitness -> sleep -> health metrics). Nutrition is
# explicitly deferred ("später") and not requested yet.
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
)


def _state_ttl_seconds() -> int:
    raw = os.getenv("HEALTH_OAUTH_STATE_TTL_SECONDS", "").strip()
    try:
        return int(raw) if raw else 600
    except ValueError:
        return 600


def client_credentials() -> tuple[str, str, str]:
    client_id = os.getenv("GOOGLE_HEALTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_HEALTH_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("GOOGLE_HEALTH_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise HealthIntegrationError(
            HEALTH_NOT_CONFIGURED,
            "Google Health ist serverseitig nicht konfiguriert (GOOGLE_HEALTH_CLIENT_ID/"
            "GOOGLE_HEALTH_CLIENT_SECRET/GOOGLE_HEALTH_REDIRECT_URI fehlen).",
        )
    return client_id, client_secret, redirect_uri


def authorization_endpoint() -> str:
    return os.getenv("GOOGLE_HEALTH_AUTHORIZATION_URL", "").strip() or DEFAULT_AUTHORIZATION_ENDPOINT


def token_endpoint() -> str:
    return os.getenv("GOOGLE_HEALTH_TOKEN_URL", "").strip() or DEFAULT_TOKEN_ENDPOINT


def revoke_endpoint() -> str:
    return os.getenv("GOOGLE_HEALTH_REVOKE_URL", "").strip() or DEFAULT_REVOKE_ENDPOINT


def api_base() -> str:
    return os.getenv("GOOGLE_HEALTH_API_BASE_URL", "").strip() or DEFAULT_API_BASE


def default_scopes() -> tuple[str, ...]:
    raw = os.getenv("GOOGLE_HEALTH_SCOPES", "").strip()
    if not raw:
        return DEFAULT_SCOPES
    return tuple(s.strip() for s in raw.split(",") if s.strip())


# ---------------------------------------------------------------------------
# DB-backed OAuth state
# ---------------------------------------------------------------------------


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_oauth_state(
    *,
    user_id: int,
    requested_scopes: tuple[str, ...],
    frontend_redirect_path: str = "/dashboard",
) -> str:
    """Creates and persists a new single-use OAuth state, returning the
    opaque value to send to Google. Only the state's hash is stored."""
    state_value = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    row = {
        "state_hash": _hash_state(state_value),
        "user_id": user_id,
        "provider": PROVIDER,
        "requested_scopes": list(requested_scopes),
        "frontend_redirect_path": frontend_redirect_path,
        "expires_at": (now + timedelta(seconds=_state_ttl_seconds())).isoformat(),
    }
    supabase.table(OAUTH_STATE_TABLE).insert(row).execute()
    return state_value


def consume_oauth_state(state_value: str) -> dict[str, object]:
    """Validates and marks a state as used. Raises on any invalid/expired/
    already-used state instead of ever silently accepting one."""
    state_hash = _hash_state(state_value)
    rows = (
        supabase.table(OAUTH_STATE_TABLE)
        .select("*")
        .eq("state_hash", state_hash)
        .eq("provider", PROVIDER)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise HealthIntegrationError(HEALTH_OAUTH_STATE_INVALID, "Ungültiger State-Parameter.")

    row = rows[0]
    if row.get("used_at"):
        raise HealthIntegrationError(HEALTH_OAUTH_STATE_USED, "State-Parameter wurde bereits verwendet.")

    expires_at_raw = str(row.get("expires_at", ""))
    try:
        expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
    except ValueError:
        raise HealthIntegrationError(HEALTH_OAUTH_STATE_INVALID, "State-Parameter ist beschädigt.")
    if datetime.now(timezone.utc) >= expires_at:
        raise HealthIntegrationError(HEALTH_OAUTH_STATE_EXPIRED, "State-Parameter ist abgelaufen. Bitte erneut verbinden.")

    # Mark used immediately — a concurrent second consumption attempt with
    # the same value will find `used_at` already set (best-effort;
    # supabase-py has no atomic compare-and-swap, see health_token_service.py
    # for the same documented limitation applied to token refresh).
    supabase.table(OAUTH_STATE_TABLE).update(
        {"used_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", row["id"]).execute()

    return row


# ---------------------------------------------------------------------------
# OAuth 2.0 Authorization Code flow
# ---------------------------------------------------------------------------


def build_authorization_url(*, state: str, scopes: tuple[str, ...] | None = None) -> str:
    client_id, _secret, redirect_uri = client_credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # offline -> issue a refresh token; consent -> always show the
        # consent screen (needed to reliably get a refresh token every
        # time); include_granted_scopes -> incremental authorization keeps
        # previously granted scopes instead of narrowing them.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "scope": " ".join(scopes or default_scopes()),
        "state": state,
    }
    return f"{authorization_endpoint()}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str, *, transport: httpx.BaseTransport | None = None) -> dict[str, object]:
    client_id, client_secret, redirect_uri = client_credentials()
    async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
        response = await client.post(
            token_endpoint(),
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        raise HealthIntegrationError(HEALTH_TOKEN_EXCHANGE_FAILED, "Google hat den Autorisierungscode abgelehnt.")
    return response.json()


async def refresh_access_token(refresh_token: str, *, transport: httpx.BaseTransport | None = None) -> dict[str, object]:
    from .health_errors import HEALTH_REFRESH_FAILED

    client_id, client_secret, _redirect_uri = client_credentials()
    async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
        response = await client.post(
            token_endpoint(),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code != 200:
        raise HealthIntegrationError(
            HEALTH_REFRESH_FAILED, "Access Token konnte nicht erneuert werden — Verbindung ggf. widerrufen."
        )
    return response.json()


async def revoke_token(token: str, *, transport: httpx.BaseTransport | None = None) -> None:
    """Best-effort — local disconnection proceeds regardless of whether
    Google's revoke call succeeds (e.g. token already invalid)."""
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            await client.post(revoke_endpoint(), params={"token": token})
    except Exception:
        pass
