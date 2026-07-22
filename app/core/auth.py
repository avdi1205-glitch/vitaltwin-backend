"""Centralized authentication, user-resolution, and ownership helpers.

Twin Intelligence Core — Etappe 2 (Nutzertrennung).

Every router that needs to know "which user is making this request?" should
use `require_email` / `require_user` from this module instead of
re-implementing its own copy of "read Authorization header -> decode JWT ->
look up user" (previously duplicated per-router, e.g. `_require_email` in
`routers/profile.py`).

Design decisions:

- The JWT itself is still decoded by `routers.users.get_email_by_token` (the
  existing, already-working, already-tested token-issuing/verifying logic is
  intentionally left untouched here — this module only *wraps* it with the
  additional user-id resolution and ownership-check helpers that Etappe 2
  needs). This keeps the change low-risk: nothing about how tokens are
  created or verified changes.
- `userId` is NEVER accepted as a request body/query parameter for
  authorization decisions. It is always derived here, server-side, from the
  verified session token — the frontend never sends it, and if it ever did,
  this module would ignore it.
- `assert_owns` returns 404 (not 403) on a mismatch, so that a guessed/
  manipulated id cannot be used to distinguish "this exists but isn't yours"
  from "this does not exist" (basic anti-enumeration hardening).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

from .supabase import supabase

USER_TABLE = "vt_users"


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated principal for the current request.

    `user_id` is the stable `vt_users.id` (bigint) — every new Twin
    Intelligence table (Etappe 2+) keys its `user_id` foreign key on this
    value. `email` is kept alongside because the pre-existing tables
    (`vt_user_profiles`, `vt_habits`, `vt_daily_wellness_entries`, ...) are
    still scoped by email (see Etappe 1 report) until they are backfilled.
    """

    email: str
    user_id: int | None


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")
    return token


def require_email(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: resolve the authenticated user's email or raise 401.

    Drop-in replacement for the `_require_email` copies duplicated across
    routers.
    """
    # Imported lazily to avoid a circular import at module load time
    # (routers.users imports from core.supabase / core.rate_limit).
    from ..routers.users import get_email_by_token

    token = _extract_bearer_token(authorization)
    email = get_email_by_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Session abgelaufen")
    return email


def get_user_id_by_email(email: str) -> int | None:
    """Look up the stable `vt_users.id` for an email.

    Returns None if the row can't be found or Supabase is unreachable —
    callers must treat that as "user id not yet resolvable", never silently
    as "this user owns nothing" (an unreachable database must never widen
    access).
    """
    try:
        response = (
            supabase.table(USER_TABLE).select("id").eq("email", email).limit(1).execute()
        )
        rows = response.data or []
        return int(rows[0]["id"]) if rows else None
    except Exception:
        return None


def require_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """FastAPI dependency: resolves the full authenticated principal
    (email + stable user_id) from the session token.

    Use this for every new Twin Intelligence endpoint instead of accepting a
    user id from the client.
    """
    email = require_email(authorization)
    user_id = get_user_id_by_email(email)
    return CurrentUser(email=email, user_id=user_id)


def assert_owns(resource_user_id: int | None, current: CurrentUser) -> None:
    """Ownership check for a single persisted record.

    Raises 404 (never 403) on any mismatch or missing id, so a manipulated or
    guessed numeric id can never be used to probe whether a record exists.
    """
    if resource_user_id is None or current.user_id is None or resource_user_id != current.user_id:
        raise HTTPException(status_code=404, detail="Nicht gefunden")
