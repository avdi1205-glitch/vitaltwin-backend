"""Token validity/refresh service — ensures every outgoing Google Health API
call uses a non-expired access token, refreshing (and re-encrypting +
re-storing) it first if needed.

Race-condition handling: see `health_connections_repository.py` module
docstring for why this is a best-effort conditional-update lock, not a true
Postgres advisory lock. On lock-acquisition failure this re-reads the
connection row (another concurrent request most likely just refreshed it)
before falling back to refreshing anyway — a duplicate refresh is safe
(Google's token endpoint tolerates a still-valid refresh token being used
again) whereas failing the request outright is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import health_connections_repository as repo
from .health_encryption_service import EncryptionFailedError, decrypt_secret, encrypt_secret
from .health_errors import HEALTH_REAUTH_REQUIRED, HEALTH_TOKEN_DECRYPT_FAILED, HealthIntegrationError
from .health_oauth_service import refresh_access_token

EXPIRY_SAFETY_MARGIN = timedelta(minutes=2)


def _is_expired(connection: dict[str, object]) -> bool:
    raw = str(connection.get("access_token_expires_at", ""))
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= expires_at - EXPIRY_SAFETY_MARGIN


async def get_valid_access_token(connection: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Returns (access_token, possibly-refreshed connection row)."""
    connection_id = int(connection["id"])  # type: ignore[arg-type]

    if not _is_expired(connection):
        try:
            return decrypt_secret(str(connection["encrypted_access_token"])), connection
        except EncryptionFailedError as exc:
            raise HealthIntegrationError(HEALTH_TOKEN_DECRYPT_FAILED, str(exc)) from exc

    lock_token = repo.try_acquire_refresh_lock(connection_id)
    if not lock_token:
        # Someone else is (or just finished) refreshing — re-read and use
        # their result if it's now valid, otherwise fall through and refresh
        # ourselves rather than fail the request.
        fresh = repo.get_connection_by_id(connection_id)
        if fresh and not _is_expired(fresh):
            try:
                return decrypt_secret(str(fresh["encrypted_access_token"])), fresh
            except EncryptionFailedError as exc:
                raise HealthIntegrationError(HEALTH_TOKEN_DECRYPT_FAILED, str(exc)) from exc
        lock_token = repo.try_acquire_refresh_lock(connection_id)

    try:
        try:
            refresh_token_plain = decrypt_secret(str(connection["encrypted_refresh_token"]))
        except EncryptionFailedError as exc:
            raise HealthIntegrationError(HEALTH_TOKEN_DECRYPT_FAILED, str(exc)) from exc

        try:
            tokens = await refresh_access_token(refresh_token_plain)
        except HealthIntegrationError:
            # invalid_grant and similar — the refresh token itself is no
            # longer usable (revoked by the user on Google's side, expired
            # test-mode 7-day refresh token, ...). Never keep retrying with
            # a dead token — require the user to reconnect.
            repo.mark_reauthorization_required(connection_id, "refresh_token_rejected")
            raise HealthIntegrationError(
                HEALTH_REAUTH_REQUIRED, "Google-Health-Verbindung ist abgelaufen. Bitte erneut verbinden."
            )

        new_access_token = str(tokens["access_token"])
        expires_in = int(tokens.get("expires_in", 3600))
        # Google does not always return a new refresh_token on refresh —
        # keep reusing the existing one unless a new one was actually issued.
        new_refresh_token = str(tokens.get("refresh_token") or refresh_token_plain)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        repo.update_tokens(
            connection_id,
            encrypted_access_token=encrypt_secret(new_access_token),
            encrypted_refresh_token=encrypt_secret(new_refresh_token),
            access_token_expires_at=expires_at,
        )
        refreshed = repo.get_connection_by_id(connection_id) or connection
        return new_access_token, refreshed
    finally:
        if lock_token:
            repo.release_refresh_lock(connection_id, lock_token)
