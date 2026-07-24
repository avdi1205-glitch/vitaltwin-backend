"""Session-expiry test — Etappe 10 §2 ("abgelaufene Session").

Uses a real, correctly-signed JWT with an `exp` claim in the past to prove
`get_email_by_token`/`require_email` actually reject expired sessions (not
just malformed tokens, which `test_auth.py` already covers) — no mocking
of the JWT library itself, only the token's expiry timestamp."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from app.core.auth import require_email
from app.routers.users import JWT_ALGORITHM, JWT_SECRET_KEY, get_email_by_token


def _make_token(*, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": "user-a@example.com", "iat": now, "exp": now + expires_delta}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


class TestExpiredSession:
    def test_expired_token_resolves_to_no_email(self):
        expired_token = _make_token(expires_delta=timedelta(days=-1))
        assert get_email_by_token(expired_token) is None

    def test_expired_token_is_rejected_with_401(self):
        expired_token = _make_token(expires_delta=timedelta(seconds=-1))
        with pytest.raises(HTTPException) as exc_info:
            require_email(f"Bearer {expired_token}")
        assert exc_info.value.status_code == 401

    def test_still_valid_token_resolves_normally(self):
        valid_token = _make_token(expires_delta=timedelta(days=30))
        assert get_email_by_token(valid_token) == "user-a@example.com"

    def test_tampered_signature_is_rejected(self):
        valid_token = _make_token(expires_delta=timedelta(days=30))
        header, payload, signature = valid_token.split(".")
        # Flip a character in the middle of the signature rather than the
        # last character — the last base64url character of a JWT signature
        # can, depending on padding/bit alignment, decode to the same
        # underlying bytes even when changed, making that particular
        # position an unreliable place to test tampering. A middle
        # character always changes the decoded signature bytes.
        mid = len(signature) // 2
        flipped_char = "A" if signature[mid] != "A" else "B"
        tampered_signature = signature[:mid] + flipped_char + signature[mid + 1 :]
        tampered = f"{header}.{payload}.{tampered_signature}"
        assert get_email_by_token(tampered) is None
