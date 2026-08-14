"""Unit tests for the customer-facing beta application status lookup
(`GET /api/beta/status`, migration 039's new `status` column) and the
`status` field now included in `POST /api/beta/apply` responses. Mocks
Supabase and rate limiting — no real network/database access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import beta as beta_module


class _FakeTable:
    def __init__(self, data):
        self._data = data
        self.inserted: list[dict] = []

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeSupabase:
    def __init__(self, rows):
        self._table = _FakeTable(rows)

    def table(self, name):
        assert name == "vt_beta_applications"
        return self._table


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(beta_module, "enforce_rate_limit", lambda *a, **k: None)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
class TestBetaApplicationStatus:
    async def test_returns_not_applied_for_unknown_email(self, monkeypatch):
        monkeypatch.setattr(beta_module, "supabase_admin", _FakeSupabase([]))
        result = await beta_module.beta_application_status(email="nobody@example.com", request=object())
        assert result == {"applied": False, "status": None}

    async def test_returns_pending_status(self, monkeypatch):
        monkeypatch.setattr(beta_module, "supabase_admin", _FakeSupabase([{"status": "pending"}]))
        result = await beta_module.beta_application_status(email="user@example.com", request=object())
        assert result == {"applied": True, "status": "pending"}

    async def test_returns_approved_status(self, monkeypatch):
        monkeypatch.setattr(beta_module, "supabase_admin", _FakeSupabase([{"status": "approved"}]))
        result = await beta_module.beta_application_status(email="user@example.com", request=object())
        assert result == {"applied": True, "status": "approved"}

    async def test_rejects_invalid_email_format(self, monkeypatch):
        monkeypatch.setattr(beta_module, "supabase_admin", _FakeSupabase([]))
        with pytest.raises(HTTPException) as exc:
            await beta_module.beta_application_status(email="not-an-email", request=object())
        assert exc.value.status_code == 400

    async def test_fails_closed_with_503_when_service_role_key_not_configured(self, monkeypatch):
        monkeypatch.setattr(beta_module, "supabase_admin", None)
        with pytest.raises(HTTPException) as exc:
            await beta_module.beta_application_status(email="user@example.com", request=object())
        assert exc.value.status_code == 503


@pytest.mark.anyio
class TestApplyIncludesStatus:
    async def test_new_application_response_includes_pending_status(self, monkeypatch):
        monkeypatch.setattr(beta_module, "_db_has_application", lambda email, locale: False)
        monkeypatch.setattr(beta_module, "_db_store_application", lambda data, locale: True)
        req = beta_module.BetaApplicationRequest(
            full_name="Test User", email="new@example.com", motivation="I want to try VitalTwin's beta."
        )
        result = await beta_module.apply_for_beta(req, request=object())
        assert result["status"] == "pending"
        assert result["already_applied"] is False

    async def test_already_applied_response_includes_real_status(self, monkeypatch):
        monkeypatch.setattr(beta_module, "_db_has_application", lambda email, locale: True)
        monkeypatch.setattr(beta_module, "_db_application_status", lambda email, locale: "approved")
        req = beta_module.BetaApplicationRequest(
            full_name="Test User", email="existing@example.com", motivation="I already applied before."
        )
        result = await beta_module.apply_for_beta(req, request=object())
        assert result["already_applied"] is True
        assert result["status"] == "approved"


@pytest.mark.anyio
class TestPrivilegedClientUsage:
    async def test_store_application_uses_service_role_client_when_configured(self, monkeypatch):
        fake = _FakeSupabase([])
        monkeypatch.setattr(beta_module, "supabase_admin", fake)
        saved = beta_module._db_store_application({"email": "new@example.com"}, "de")
        assert saved is True
        assert fake._table.inserted == [{"email": "new@example.com"}]

    async def test_store_application_fails_closed_without_service_role_key(self, monkeypatch):
        monkeypatch.setattr(beta_module, "supabase_admin", None)
        with pytest.raises(HTTPException) as exc:
            beta_module._db_store_application({"email": "new@example.com"}, "en")
        assert exc.value.status_code == 503

    async def test_apply_returns_503_without_service_role_key_configured(self, monkeypatch):
        monkeypatch.setattr(beta_module, "supabase_admin", None)
        req = beta_module.BetaApplicationRequest(
            full_name="Test User", email="new@example.com", motivation="Testing the fail-closed path."
        )
        with pytest.raises(HTTPException) as exc:
            await beta_module.apply_for_beta(req, request=object())
        assert exc.value.status_code == 503
