"""Structured internal error codes for the Google Health integration —
every raised `HealthIntegrationError` carries one of these, mapped to an
appropriate HTTP status in `routers/google_health.py`. Never leaks raw
Google error payloads or tokens to the client — only these safe, stable
internal codes plus a human-readable (non-sensitive) message.
"""

from __future__ import annotations


class HealthIntegrationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# OAuth
HEALTH_OAUTH_DENIED = "HEALTH_OAUTH_DENIED"
HEALTH_OAUTH_STATE_INVALID = "HEALTH_OAUTH_STATE_INVALID"
HEALTH_OAUTH_STATE_EXPIRED = "HEALTH_OAUTH_STATE_EXPIRED"
HEALTH_OAUTH_STATE_USED = "HEALTH_OAUTH_STATE_USED"
HEALTH_TOKEN_EXCHANGE_FAILED = "HEALTH_TOKEN_EXCHANGE_FAILED"
HEALTH_NO_REFRESH_TOKEN = "HEALTH_NO_REFRESH_TOKEN"

# Connection / token state
HEALTH_NOT_CONNECTED = "HEALTH_NOT_CONNECTED"
HEALTH_REAUTH_REQUIRED = "HEALTH_REAUTH_REQUIRED"
HEALTH_SCOPE_MISSING = "HEALTH_SCOPE_MISSING"
HEALTH_REFRESH_FAILED = "HEALTH_REFRESH_FAILED"
HEALTH_TOKEN_DECRYPT_FAILED = "HEALTH_TOKEN_DECRYPT_FAILED"
HEALTH_NOT_CONFIGURED = "HEALTH_NOT_CONFIGURED"

# Provider / API
HEALTH_RATE_LIMITED = "HEALTH_RATE_LIMITED"
HEALTH_PROVIDER_UNAVAILABLE = "HEALTH_PROVIDER_UNAVAILABLE"
HEALTH_PROVIDER_ERROR = "HEALTH_PROVIDER_ERROR"
HEALTH_UNSUPPORTED_DATA_TYPE = "HEALTH_UNSUPPORTED_DATA_TYPE"

# Sync
HEALTH_SYNC_PARTIAL = "HEALTH_SYNC_PARTIAL"
HEALTH_SYNC_FAILED = "HEALTH_SYNC_FAILED"

# Access control
HEALTH_FORBIDDEN = "HEALTH_FORBIDDEN"
