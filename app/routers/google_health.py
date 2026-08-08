"""Google Health integration — provider/connection/OAuth/sync/data endpoints.

Mounted at `/api/health` in `app/main.py` — the SAME top-level prefix as the
existing `routers/health.py` (CGM/nutrition). Confirmed collision-free: that
router only defines `/cgm/*` and `/nutrition/*`, this one only defines
`/providers`, `/connections`, `/google/*`, and `/data/*`.

Every authenticated endpoint uses `core.auth.require_user` (not
`require_email`) since the new tables key on the stable `vt_users.id`, and
every lookup is scoped to `current.user_id` — there is no endpoint that
accepts a connection id or user id from the client for authorization
purposes. A cross-user lookup miss returns 404 (never 403), matching this
codebase's `assert_owns` anti-enumeration convention.

`GET /google/connect` returns a JSON `{authorization_url}` body rather than
issuing an HTTP redirect itself — a deliberate decision (documented in
`GOOGLE_HEALTH_API_AUDIT.md`) because the frontend calls this endpoint via
`fetch()` (to send the `Authorization` bearer header) and then performs the
actual browser navigation itself via `window.location.href = authorization_url`.
`GET /google/callback` is the one endpoint Google itself redirects the
user's browser to directly, so it real-redirects back to the frontend.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..core import health_connections_repository as repo
from ..core import health_oauth_service as oauth
from ..core.auth import require_user
from ..core.health_encryption_service import EncryptionFailedError, decrypt_secret, encrypt_secret
from ..core.health_errors import (
    HEALTH_NOT_CONNECTED,
    HEALTH_NO_REFRESH_TOKEN,
    HEALTH_OAUTH_DENIED,
    HEALTH_REAUTH_REQUIRED,
    HealthIntegrationError,
)
from ..core.health_normalization_service import DATA_TYPE_CONFIG
from ..core.health_sync_service import sync_user_health_data
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase

router = APIRouter()

_ERROR_STATUS: dict[str, int] = {
    "HEALTH_NOT_CONFIGURED": 500,
    "HEALTH_OAUTH_DENIED": 400,
    "HEALTH_OAUTH_STATE_INVALID": 400,
    "HEALTH_OAUTH_STATE_EXPIRED": 400,
    "HEALTH_OAUTH_STATE_USED": 400,
    "HEALTH_TOKEN_EXCHANGE_FAILED": 502,
    "HEALTH_NO_REFRESH_TOKEN": 400,
    "HEALTH_NOT_CONNECTED": 404,
    "HEALTH_REAUTH_REQUIRED": 409,
    "HEALTH_SCOPE_MISSING": 403,
    "HEALTH_REFRESH_FAILED": 502,
    "HEALTH_TOKEN_DECRYPT_FAILED": 500,
    "HEALTH_RATE_LIMITED": 429,
    "HEALTH_PROVIDER_UNAVAILABLE": 502,
    "HEALTH_PROVIDER_ERROR": 502,
    "HEALTH_UNSUPPORTED_DATA_TYPE": 400,
    "HEALTH_SYNC_FAILED": 502,
    "HEALTH_FORBIDDEN": 404,
}


def _raise(exc: HealthIntegrationError) -> None:
    raise HTTPException(status_code=_ERROR_STATUS.get(exc.code, 502), detail={"code": exc.code, "message": exc.message})


def _frontend_url(path: str) -> str:
    base = os.getenv("FRONTEND_BASE_URL", "https://www.vitaltwin.de").rstrip("/")
    return f"{base}{path}"


def _connection_public_view(connection: dict[str, object]) -> dict[str, object]:
    """Never includes any token field — status metadata only."""
    return {
        "provider": connection.get("provider"),
        "status": connection.get("status"),
        "connected_at": connection.get("connected_at"),
        "granted_scopes": connection.get("granted_scopes"),
        "reauthorization_required_at": connection.get("reauthorization_required_at"),
        "last_sync_at": connection.get("last_sync_at"),
        "last_sync_status": connection.get("last_sync_status"),
        "last_sync_error_code": connection.get("last_sync_error_code"),
    }


def _require_user_id(authorization: str | None) -> int:
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")
    return current.user_id


@router.get("/providers")
async def list_health_providers(authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)
    connection = repo.get_any_connection(user_id)
    return {
        "providers": [
            {
                "id": "google_health",
                "name": "Google Health",
                "status": connection.get("status") if connection else "not_connected",
            }
        ]
    }


@router.get("/connections")
async def list_health_connections(authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)
    connection = repo.get_any_connection(user_id)
    return {"connections": [_connection_public_view(connection)] if connection else []}


@router.get("/google/connect")
async def start_google_health_connect(request: Request, authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)
    enforce_rate_limit(request, "google_health_connect", max_requests=10, window_seconds=300)

    try:
        scopes = oauth.default_scopes()
        state = oauth.create_oauth_state(user_id=user_id, requested_scopes=scopes)
        authorization_url = oauth.build_authorization_url(state=state, scopes=scopes)
    except HealthIntegrationError as exc:
        _raise(exc)

    return {"authorization_url": authorization_url}


@router.get("/google/callback")
async def google_health_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(_frontend_url(f"/dashboard?health_connect=error&reason={HEALTH_OAUTH_DENIED}"))
    if not code or not state:
        return RedirectResponse(_frontend_url("/dashboard?health_connect=error&reason=HEALTH_OAUTH_STATE_INVALID"))

    try:
        state_row = oauth.consume_oauth_state(state)
        user_id = int(state_row["user_id"])
        redirect_path = str(state_row.get("frontend_redirect_path") or "/dashboard")

        tokens = await oauth.exchange_code_for_tokens(code)
        access_token = str(tokens["access_token"])
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            # Happens if the user had already granted consent before and
            # Google skipped re-issuing a refresh token despite prompt=consent
            # — without one we can't do offline sync, so treat as an error
            # instead of silently storing a connection that will break in an hour.
            raise HealthIntegrationError(HEALTH_NO_REFRESH_TOKEN, "Kein Refresh Token erhalten.")

        granted_scope_string = str(tokens.get("scope", ""))
        granted_scopes = [s for s in granted_scope_string.split(" ") if s]

        provider_health_user_id = None
        provider_legacy_user_id = None
        try:
            from ..core.google_health_client import GoogleHealthClient

            identity = await GoogleHealthClient(access_token=access_token).get_identity()
            provider_health_user_id = identity.get("healthUserId") or identity.get("userId")  # type: ignore[assignment]
            provider_legacy_user_id = identity.get("legacyUserId") or identity.get("fitbitUserId")  # type: ignore[assignment]
        except HealthIntegrationError:
            pass  # identity is nice-to-have, not required to store the connection

        expires_in = int(tokens.get("expires_in", 3600))
        from datetime import datetime, timedelta, timezone

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        repo.upsert_connection(
            user_id=user_id,
            encrypted_access_token=encrypt_secret(access_token),
            encrypted_refresh_token=encrypt_secret(str(refresh_token)),
            access_token_expires_at=expires_at,
            granted_scopes=granted_scopes,
            provider_health_user_id=provider_health_user_id,
            provider_legacy_user_id=provider_legacy_user_id,
        )

        requested_scopes = state_row.get("requested_scopes") or []
        missing_scopes = [s for s in requested_scopes if s not in granted_scopes]
        reason = "success" if not missing_scopes else "partial_consent"
        return RedirectResponse(_frontend_url(f"{redirect_path}?health_connect={reason}"))
    except HealthIntegrationError as exc:
        return RedirectResponse(_frontend_url(f"/dashboard?health_connect=error&reason={exc.code}"))


@router.get("/google/status")
async def google_health_status(authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)
    connection = repo.get_any_connection(user_id)
    if not connection:
        return {"connected": False, "status": "not_connected"}
    return {"connected": connection.get("status") == "connected", **_connection_public_view(connection)}


@router.post("/google/disconnect")
async def google_health_disconnect(authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)
    connection = repo.get_any_connection(user_id)
    if not connection or connection.get("status") == "disconnected":
        raise HTTPException(
            status_code=_ERROR_STATUS[HEALTH_NOT_CONNECTED],
            detail={"code": HEALTH_NOT_CONNECTED, "message": "Kein aktives Google-Health-Konto verbunden."},
        )
    try:
        refresh_token = decrypt_secret(str(connection["encrypted_refresh_token"]))
        await oauth.revoke_token(refresh_token)
    except EncryptionFailedError:
        pass
    repo.disconnect_connection(int(connection["id"]))  # type: ignore[arg-type]
    return {"disconnected": True}


@router.delete("/google/data")
async def google_health_delete_data(authorization: str | None = Header(default=None)):
    """Optional, separate from disconnect: purges previously synced data
    without necessarily revoking the OAuth connection itself."""
    user_id = _require_user_id(authorization)
    for table in ("health_activity_records", "health_sleep_records", "health_metric_records"):
        supabase.table(table).delete().eq("user_id", user_id).execute()
    return {"deleted": True}


@router.post("/google/sync")
async def google_health_sync(request: Request, authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)
    enforce_rate_limit(request, "google_health_sync", max_requests=12, window_seconds=3600)

    connection = repo.get_active_connection(user_id)
    if not connection:
        raise HTTPException(
            status_code=_ERROR_STATUS[HEALTH_NOT_CONNECTED],
            detail={"code": HEALTH_NOT_CONNECTED, "message": "Kein aktives Google-Health-Konto verbunden."},
        )
    if connection.get("status") == "reauthorization_required":
        raise HTTPException(
            status_code=_ERROR_STATUS[HEALTH_REAUTH_REQUIRED],
            detail={"code": HEALTH_REAUTH_REQUIRED, "message": "Verbindung ist abgelaufen. Bitte erneut verbinden."},
        )

    try:
        result = await sync_user_health_data(user_id=user_id, connection=connection)
    except HealthIntegrationError as exc:
        _raise(exc)
        return  # unreachable, keeps type-checkers happy

    return result


def _query_records(*, table: str, user_id: int, data_type: str | None, limit: int, order_column: str):
    query = supabase.table(table).select("*").eq("user_id", user_id)
    if data_type:
        query = query.eq("data_type", data_type)
    rows = query.order(order_column, desc=True).limit(max(1, min(limit, 500))).execute().data or []
    return rows


@router.get("/data/activity")
async def get_activity_data(
    data_type: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)
):
    user_id = _require_user_id(authorization)
    if data_type and (data_type not in DATA_TYPE_CONFIG or DATA_TYPE_CONFIG[data_type].category != "activity"):
        raise HTTPException(status_code=400, detail="Ungültiger data_type für Aktivitätsdaten.")
    items = _query_records(
        table="health_activity_records", user_id=user_id, data_type=data_type, limit=limit, order_column="start_time"
    )
    return {"items": items}


@router.get("/data/sleep")
async def get_sleep_data(limit: int = 100, authorization: str | None = Header(default=None)):
    user_id = _require_user_id(authorization)
    items = _query_records(
        table="health_sleep_records", user_id=user_id, data_type=None, limit=limit, order_column="start_time"
    )
    return {"items": items}


@router.get("/data/metrics")
async def get_metric_data(
    data_type: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)
):
    user_id = _require_user_id(authorization)
    if data_type and (data_type not in DATA_TYPE_CONFIG or DATA_TYPE_CONFIG[data_type].category != "metric"):
        raise HTTPException(status_code=400, detail="Ungültiger data_type für Metrik-Daten.")
    items = _query_records(
        table="health_metric_records", user_id=user_id, data_type=data_type, limit=limit, order_column="observed_at"
    )
    return {"items": items}
