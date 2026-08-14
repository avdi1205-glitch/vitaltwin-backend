"""Unit tests for the one-click beta application review workflow
(`approve_beta_application`/`reject_beta_application` in `app.routers.admin`)
— the piece that actually connects a customer's self-service application
(`vt_beta_applications`, migration 039's new `status` column) to the
EXISTING admin-controlled Beta Tester Program overlay (`grant_beta_by_email`).
Mocks Supabase and `require_admin_permission` — no real network/database
access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.admin_rbac import AdminPrincipal
from app.routers import admin as admin_module


class _FakeTable:
    def __init__(self, data):
        self._data = data
        self._updates: list[dict] = []

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def update(self, payload):
        self._updates.append(payload)
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeSupabase:
    def __init__(self, application_rows=None, user_rows=None):
        self._application = _FakeTable(application_rows if application_rows is not None else [])
        self._users = _FakeTable(user_rows if user_rows is not None else [])

    def table(self, name):
        if name == "vt_beta_applications":
            return self._application
        if name == "vt_users":
            return self._users
        raise AssertionError(f"unexpected table {name}")


@pytest.fixture
def admin_principal():
    return AdminPrincipal(email="founder@example.com", role="super_admin")


@pytest.fixture
def permission_spy(monkeypatch, admin_principal):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return admin_principal

    monkeypatch.setattr(admin_module, "require_admin_permission", _fake)
    return calls


@pytest.fixture
def recorded_audit_events(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(admin_module, "record_audit_event", lambda **kwargs: events.append(kwargs))
    return events


@pytest.mark.anyio
class TestApproveBetaApplication:
    async def test_requires_manage_premium_permission(self, monkeypatch, permission_spy):
        fake = _FakeSupabase(
            application_rows=[{"id": 1, "email": "user@example.com", "status": "pending"}],
            user_rows=[{"email": "user@example.com"}],
        )
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)
        monkeypatch.setattr(admin_module, "grant_beta_by_email", lambda *a, **k: True)
        monkeypatch.setattr(admin_module, "get_beta_grant_by_email", lambda email: {"plan": "pro", "active": True})

        await admin_module.approve_beta_application(1, authorization="Bearer x")

        assert permission_spy[0][1] == "manage_premium"

    async def test_success_grants_exactly_90_day_pro_and_marks_approved(
        self, monkeypatch, permission_spy, recorded_audit_events
    ):
        fake = _FakeSupabase(
            application_rows=[{"id": 1, "email": "user@example.com", "status": "pending"}],
            user_rows=[{"email": "user@example.com"}],
        )
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)

        grant_calls = []
        monkeypatch.setattr(
            admin_module,
            "grant_beta_by_email",
            lambda email, plan, days, granted_by: grant_calls.append((email, plan, days, granted_by)) or True,
        )
        monkeypatch.setattr(admin_module, "get_beta_grant_by_email", lambda email: {"plan": "pro", "active": True})

        result = await admin_module.approve_beta_application(1, authorization="Bearer x")

        assert grant_calls == [("user@example.com", "pro", 90, "founder@example.com")]
        assert result["email"] == "user@example.com"
        assert fake._application._updates[-1]["status"] == "approved"
        assert fake._application._updates[-1]["reviewed_by"] == "founder@example.com"
        assert recorded_audit_events[0]["metadata"]["beta_plan"] == "pro"
        assert recorded_audit_events[0]["metadata"]["days"] == 90

    async def test_404_when_application_not_found(self, monkeypatch, permission_spy):
        fake = _FakeSupabase(application_rows=[], user_rows=[{"email": "user@example.com"}])
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)

        with pytest.raises(HTTPException) as exc:
            await admin_module.approve_beta_application(999, authorization="Bearer x")
        assert exc.value.status_code == 404

    async def test_409_when_application_already_reviewed(self, monkeypatch, permission_spy):
        fake = _FakeSupabase(
            application_rows=[{"id": 1, "email": "user@example.com", "status": "approved"}],
            user_rows=[{"email": "user@example.com"}],
        )
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)

        with pytest.raises(HTTPException) as exc:
            await admin_module.approve_beta_application(1, authorization="Bearer x")
        assert exc.value.status_code == 409

    async def test_422_when_no_account_exists_for_applicant_email(self, monkeypatch, permission_spy):
        fake = _FakeSupabase(
            application_rows=[{"id": 1, "email": "no-account@example.com", "status": "pending"}],
            user_rows=[],
        )
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)
        grant_calls = []
        monkeypatch.setattr(admin_module, "grant_beta_by_email", lambda *a, **k: grant_calls.append(a) or True)

        with pytest.raises(HTTPException) as exc:
            await admin_module.approve_beta_application(1, authorization="Bearer x")
        assert exc.value.status_code == 422
        assert grant_calls == []
        assert fake._application._updates == []


@pytest.mark.anyio
class TestRejectBetaApplication:
    async def test_success_marks_rejected_with_no_entitlement_change(
        self, monkeypatch, permission_spy, recorded_audit_events
    ):
        fake = _FakeSupabase(application_rows=[{"id": 2, "status": "pending"}])
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)

        result = await admin_module.reject_beta_application(2, authorization="Bearer x")

        assert result["application_id"] == 2
        assert fake._application._updates[-1]["status"] == "rejected"
        assert recorded_audit_events[0]["metadata"]["status"] == "rejected"

    async def test_409_when_already_reviewed(self, monkeypatch, permission_spy):
        fake = _FakeSupabase(application_rows=[{"id": 2, "status": "rejected"}])
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)

        with pytest.raises(HTTPException) as exc:
            await admin_module.reject_beta_application(2, authorization="Bearer x")
        assert exc.value.status_code == 409

    async def test_404_when_not_found(self, monkeypatch, permission_spy):
        fake = _FakeSupabase(application_rows=[])
        monkeypatch.setattr(admin_module, "supabase", fake)
        monkeypatch.setattr(admin_module, "supabase_admin", fake)

        with pytest.raises(HTTPException) as exc:
            await admin_module.reject_beta_application(999, authorization="Bearer x")
        assert exc.value.status_code == 404


@pytest.mark.anyio
class TestPrivilegedClientFailsClosed:
    async def test_approve_returns_503_without_service_role_key(self, monkeypatch, permission_spy):
        monkeypatch.setattr(admin_module, "supabase_admin", None)
        with pytest.raises(HTTPException) as exc:
            await admin_module.approve_beta_application(1, authorization="Bearer x")
        assert exc.value.status_code == 503

    async def test_reject_returns_503_without_service_role_key(self, monkeypatch, permission_spy):
        monkeypatch.setattr(admin_module, "supabase_admin", None)
        with pytest.raises(HTTPException) as exc:
            await admin_module.reject_beta_application(1, authorization="Bearer x")
        assert exc.value.status_code == 503

    async def test_list_returns_empty_without_service_role_key(self, monkeypatch, permission_spy):
        monkeypatch.setattr(admin_module, "supabase_admin", None)
        with pytest.raises(HTTPException) as exc:
            await admin_module.list_beta_applications(authorization="Bearer x")
        assert exc.value.status_code == 503


@pytest.fixture
def anyio_backend():
    return "asyncio"
