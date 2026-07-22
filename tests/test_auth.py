"""Unit tests for `app.core.auth` (Twin Intelligence Core, Etappe 2 —
Nutzertrennung).

Covers the exact scenarios required by the Etappe 2 spec §5:

- unauthenticated access is rejected
- a manipulated/guessed numeric id never grants access to another user's data
- "user A cannot read/update/delete user B" via the ownership check

No real network/database access — `get_user_id_by_token`/Supabase calls are
monkeypatched.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.auth import CurrentUser, assert_owns, require_email, require_user


class TestRequireEmailRejectsUnauthenticated:
    def test_missing_header_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            require_email(None)
        assert exc_info.value.status_code == 401

    def test_non_bearer_header_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            require_email("Basic abc123")
        assert exc_info.value.status_code == 401

    def test_empty_bearer_token_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            require_email("Bearer ")
        assert exc_info.value.status_code == 401

    def test_invalid_token_raises_401(self, monkeypatch):
        # get_email_by_token is imported lazily inside require_email from
        # app.routers.users — patch it there.
        import app.routers.users as users_module

        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: None)
        with pytest.raises(HTTPException) as exc_info:
            require_email("Bearer not-a-real-jwt")
        assert exc_info.value.status_code == 401


class TestRequireEmailAcceptsValidToken:
    def test_valid_token_resolves_email(self, monkeypatch):
        import app.routers.users as users_module

        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: "user-a@example.com")
        assert require_email("Bearer valid-token") == "user-a@example.com"


class TestRequireUserResolvesUserId:
    def test_resolves_user_id_from_email(self, monkeypatch):
        import app.core.auth as auth_module
        import app.routers.users as users_module

        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: "user-a@example.com")
        monkeypatch.setattr(auth_module, "get_user_id_by_email", lambda email: 42)

        current = require_user("Bearer valid-token")
        assert current.email == "user-a@example.com"
        assert current.user_id == 42

    def test_missing_user_row_yields_none_user_id_not_an_error(self, monkeypatch):
        import app.core.auth as auth_module
        import app.routers.users as users_module

        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: "ghost@example.com")
        monkeypatch.setattr(auth_module, "get_user_id_by_email", lambda email: None)

        current = require_user("Bearer valid-token")
        assert current.user_id is None


class TestAssertOwnsBlocksCrossUserAccess:
    """The concrete "Nutzer A kann Nutzer B nicht lesen/bearbeiten/löschen"
    and "erratene IDs ermöglichen keinen Zugriff" scenarios from Etappe 2 §5."""

    def test_owner_matches_passes_silently(self):
        user_a = CurrentUser(email="a@example.com", user_id=1)
        assert_owns(1, user_a)  # does not raise

    def test_reading_another_users_record_is_rejected(self):
        user_a = CurrentUser(email="a@example.com", user_id=1)
        user_b_record_user_id = 2
        with pytest.raises(HTTPException) as exc_info:
            assert_owns(user_b_record_user_id, user_a)
        assert exc_info.value.status_code == 404

    def test_guessed_or_manipulated_id_without_resolvable_user_is_rejected(self):
        # e.g. Supabase lookup failed / user row missing -> user_id is None.
        unresolved_user = CurrentUser(email="ghost@example.com", user_id=None)
        with pytest.raises(HTTPException) as exc_info:
            assert_owns(1, unresolved_user)
        assert exc_info.value.status_code == 404

    def test_record_without_owner_is_rejected(self):
        user_a = CurrentUser(email="a@example.com", user_id=1)
        with pytest.raises(HTTPException) as exc_info:
            assert_owns(None, user_a)
        assert exc_info.value.status_code == 404

    def test_mismatch_returns_404_not_403_to_avoid_enumeration(self):
        user_a = CurrentUser(email="a@example.com", user_id=1)
        with pytest.raises(HTTPException) as exc_info:
            assert_owns(999999, user_a)
        # Deliberately 404, not 403 — see module docstring in core/auth.py.
        assert exc_info.value.status_code == 404
        assert exc_info.value.status_code != 403
