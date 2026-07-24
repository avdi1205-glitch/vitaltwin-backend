"""Unit tests for login-event logging and suspension enforcement in
`app.routers.users` (Admin Control Center: Security Center login history +
"Nutzer sperren" must actually block login, not just look cosmetic).

No real network/database access — Supabase and the rate limiter are
monkeypatched; `users_store` is used directly to control user state."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import users as users_module
from app.routers.users import LoginRequest, _record_login_event


class _FakeQuery:
    def __init__(self, log: list, raise_error: bool = False):
        self._log = log
        self._raise_error = raise_error

    def insert(self, payload):
        self._log.append(payload)
        return self

    def execute(self):
        if self._raise_error:
            raise RuntimeError("boom")
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def __init__(self, raise_error: bool = False):
        self.inserted: list = []
        self._raise_error = raise_error

    def table(self, name):
        return _FakeQuery(self.inserted, self._raise_error)


def _make_request(ip: str = "203.0.113.5", user_agent: str = "pytest-agent") -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host=ip),
        headers={"user-agent": user_agent},
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_users_store():
    users_module.users_store.clear()
    yield
    users_module.users_store.clear()


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(users_module, "enforce_rate_limit", lambda *args, **kwargs: None)


class TestRecordLoginEvent:
    def test_inserts_email_success_ip_and_user_agent(self, monkeypatch):
        fake = _FakeSupabase()
        monkeypatch.setattr(users_module, "supabase", fake)
        _record_login_event(email="user@example.com", success=True, request=_make_request())
        assert fake.inserted[-1] == {
            "email": "user@example.com",
            "success": True,
            "ip_address": "203.0.113.5",
            "user_agent": "pytest-agent",
        }

    def test_never_raises_on_db_failure(self, monkeypatch):
        fake = _FakeSupabase(raise_error=True)
        monkeypatch.setattr(users_module, "supabase", fake)
        # Must not raise — a failed audit/log write must never block the caller.
        _record_login_event(email="user@example.com", success=False, request=_make_request())

    def test_handles_missing_client_gracefully(self, monkeypatch):
        fake = _FakeSupabase()
        monkeypatch.setattr(users_module, "supabase", fake)
        request = SimpleNamespace(client=None, headers={})
        _record_login_event(email="user@example.com", success=True, request=request)
        assert fake.inserted[-1]["ip_address"] is None


class TestLoginRecordsEvents:
    @pytest.mark.anyio
    async def test_successful_login_records_success_event(self, monkeypatch):
        fake = _FakeSupabase()
        monkeypatch.setattr(users_module, "supabase", fake)
        users_module.users_store["user@example.com"] = {
            "password": users_module._hash_password("correct-password"),
            "full_name": "Test User",
            "premium": False,
        }

        result = await users_module.login(
            LoginRequest(email="user@example.com", password="correct-password"), _make_request()
        )
        assert result["access_token"]
        assert fake.inserted[-1] == {
            "email": "user@example.com",
            "success": True,
            "ip_address": "203.0.113.5",
            "user_agent": "pytest-agent",
        }

    @pytest.mark.anyio
    async def test_wrong_password_records_failure_event_and_raises_401(self, monkeypatch):
        fake = _FakeSupabase()
        monkeypatch.setattr(users_module, "supabase", fake)
        users_module.users_store["user@example.com"] = {
            "password": users_module._hash_password("correct-password"),
            "full_name": "Test User",
            "premium": False,
        }

        with pytest.raises(HTTPException) as exc_info:
            await users_module.login(
                LoginRequest(email="user@example.com", password="wrong-password"), _make_request()
            )
        assert exc_info.value.status_code == 401
        assert fake.inserted[-1]["success"] is False

    @pytest.mark.anyio
    async def test_suspended_account_login_returns_403_and_logs_failure(self, monkeypatch):
        fake = _FakeSupabase()
        monkeypatch.setattr(users_module, "supabase", fake)
        users_module.users_store["user@example.com"] = {
            "password": users_module._hash_password("correct-password"),
            "full_name": "Test User",
            "premium": False,
            "suspended": True,
        }

        with pytest.raises(HTTPException) as exc_info:
            await users_module.login(
                LoginRequest(email="user@example.com", password="correct-password"), _make_request()
            )
        assert exc_info.value.status_code == 403
        assert fake.inserted[-1]["success"] is False

    @pytest.mark.anyio
    async def test_unsuspended_account_can_still_log_in(self, monkeypatch):
        fake = _FakeSupabase()
        monkeypatch.setattr(users_module, "supabase", fake)
        users_module.users_store["user@example.com"] = {
            "password": users_module._hash_password("correct-password"),
            "full_name": "Test User",
            "premium": False,
            "suspended": False,
        }

        result = await users_module.login(
            LoginRequest(email="user@example.com", password="correct-password"), _make_request()
        )
        assert result["access_token"]
