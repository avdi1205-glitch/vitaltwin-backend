"""Unit tests for `app.routers.admin` — the Admin Control Center API.
Mocks Supabase and `require_admin_permission` — no real network/database
access. Focuses on: (1) every endpoint requests the correct permission,
(2) user-management/content/security business logic and audit-event
firing, (3) the "never select passwords" guarantee, and (4) the honest
"not implemented" notes for genuinely absent capabilities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.admin_rbac import AdminPrincipal
from app.core import ai_usage_logger, error_events, founder_backup_status, founder_releases, stripe_billing
from app.routers import admin as admin_module
from app.routers.admin import BackupInput, ContentInput, PlanChangeInput, PremiumInput, QACleanupExecuteInput, ReleaseInput, RoleInput, SuspendInput


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeQuery:
    """A minimal, permissive stand-in for the Supabase query builder that
    records every method call and returns configured data/count for the
    table it was built for."""

    def __init__(self, table_name: str, store: dict, log: list):
        self._table_name = table_name
        self._store = store
        self._log = log
        self._select_count_requested = False

    def _record(self, method, *args, **kwargs):
        self._log.append((self._table_name, method, args, kwargs))

    def select(self, *args, count=None, **kwargs):
        self._select_count_requested = count is not None
        self._record("select", *args, count=count, **kwargs)
        return self

    def eq(self, *args, **kwargs):
        self._record("eq", *args, **kwargs)
        return self

    def neq(self, *args, **kwargs):
        self._record("neq", *args, **kwargs)
        return self

    def gte(self, *args, **kwargs):
        self._record("gte", *args, **kwargs)
        return self

    def or_(self, *args, **kwargs):
        self._record("or_", *args, **kwargs)
        return self

    def in_(self, *args, **kwargs):
        self._record("in_", *args, **kwargs)
        return self

    def is_(self, *args, **kwargs):
        self._record("is_", *args, **kwargs)
        return self

    @property
    def not_(self):
        self._record("not_")
        return self

    def order(self, *args, **kwargs):
        self._record("order", *args, **kwargs)
        return self

    def range(self, *args, **kwargs):
        self._record("range", *args, **kwargs)
        return self

    def limit(self, *args, **kwargs):
        self._record("limit", *args, **kwargs)
        return self

    def insert(self, payload):
        self._record("insert", payload)
        entry = self._store.setdefault(self._table_name, {})
        entry.setdefault("inserted", []).append(payload)
        return self

    def update(self, payload):
        self._record("update", payload)
        entry = self._store.setdefault(self._table_name, {})
        entry.setdefault("updated", []).append(payload)
        return self

    def upsert(self, payload):
        self._record("upsert", payload)
        entry = self._store.setdefault(self._table_name, {})
        entry.setdefault("upserted", []).append(payload)
        return self

    def delete(self):
        self._record("delete")
        entry = self._store.setdefault(self._table_name, {})
        entry["deleted"] = True
        return self

    def execute(self):
        entry = self._store.get(self._table_name, {})
        if entry.get("raise"):
            raise RuntimeError("boom")
        data = entry.get("data", [])
        count = entry.get("count") if entry.get("count") is not None else (len(data) if self._select_count_requested else None)
        return SimpleNamespace(data=entry.get("insert_result", data), count=count)


class _FakeSupabase:
    def __init__(self, tables: dict | None = None):
        self.store = tables or {}
        self.log: list = []

    def table(self, name):
        return _FakeQuery(name, self.store, self.log)


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(admin_module, "supabase", fake)
    return fake


@pytest.fixture
def fake_internal_logging_supabase(monkeypatch):
    """`founder_releases.py`/`founder_backup_status.py`/`error_events.py`/
    `ai_usage_logger.py` each import their own `supabase` binding (same
    established pattern as every other core module in this codebase) — so
    testing admin.py endpoints that call into them requires patching those
    too, independently of `fake_supabase` (which only patches
    `admin_module.supabase`). Without this, `system_status()`/`ai_usage()`
    would silently hit the real Supabase client."""
    release_fake = _FakeSupabase()
    backup_fake = _FakeSupabase()
    error_fake = _FakeSupabase()
    ai_usage_fake = _FakeSupabase()
    stripe_fake = _FakeSupabase()
    monkeypatch.setattr(founder_releases, "supabase", release_fake)
    monkeypatch.setattr(founder_backup_status, "supabase", backup_fake)
    monkeypatch.setattr(error_events, "supabase", error_fake)
    monkeypatch.setattr(ai_usage_logger, "supabase", ai_usage_fake)
    monkeypatch.setattr(stripe_billing, "supabase", stripe_fake)
    return SimpleNamespace(release=release_fake, backup=backup_fake, error=error_fake, ai_usage=ai_usage_fake, stripe=stripe_fake)


@pytest.fixture
def super_admin_principal():
    return AdminPrincipal(email="admin@example.com", role="super_admin")


@pytest.fixture
def permission_spy(monkeypatch, super_admin_principal):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return super_admin_principal

    monkeypatch.setattr(admin_module, "require_admin_permission", _fake)
    return calls


@pytest.fixture
def recorded_audit_events(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(admin_module, "record_audit_event", lambda **kwargs: events.append(kwargs))
    return events


# ---------------------------------------------------------------------------
# Every endpoint must request the correct permission
# ---------------------------------------------------------------------------


class TestGetCurrentAdmin:
    @pytest.mark.anyio
    async def test_returns_own_role_and_permission_list(self, monkeypatch):
        from app.core.admin_rbac import AdminPrincipal as _Principal

        monkeypatch.setattr(
            admin_module, "require_admin", lambda auth: _Principal(email="editor@example.com", role="editor")
        )
        result = await admin_module.get_current_admin(authorization="Bearer x")
        assert result["email"] == "editor@example.com"
        assert result["role"] == "editor"
        assert result["permissions"] == ["manage_content", "view_content"]


class TestPermissionRequirements:
    @pytest.mark.anyio
    async def test_dashboard_requires_view_dashboard(self, fake_supabase, fake_internal_logging_supabase, permission_spy):
        await admin_module.admin_dashboard(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_dashboard")

    @pytest.mark.anyio
    async def test_list_users_requires_view_users(self, fake_supabase, permission_spy):
        await admin_module.list_users(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_users")

    @pytest.mark.anyio
    async def test_suspend_requires_manage_users(self, fake_supabase, permission_spy, recorded_audit_events):
        await admin_module.suspend_user("user@example.com", SuspendInput(reason="spam"), authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "manage_users")

    @pytest.mark.anyio
    async def test_set_role_requires_manage_roles(self, fake_supabase, permission_spy, recorded_audit_events):
        await admin_module.set_user_role("user@example.com", RoleInput(role="support"), authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "manage_roles")

    @pytest.mark.anyio
    async def test_set_premium_requires_manage_premium(self, monkeypatch, fake_supabase, permission_spy, recorded_audit_events):
        monkeypatch.setattr(admin_module, "set_premium_by_email", lambda email, premium: True)
        await admin_module.set_user_premium("user@example.com", PremiumInput(premium=True), authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "manage_premium")

    @pytest.mark.anyio
    async def test_audit_logs_requires_view_security(self, fake_supabase, permission_spy):
        await admin_module.get_audit_logs(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_security")

    @pytest.mark.anyio
    async def test_permission_matrix_requires_view_security(self, fake_supabase, permission_spy):
        await admin_module.get_permission_matrix(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_security")

    @pytest.mark.anyio
    async def test_system_status_requires_view_system_status(self, fake_supabase, permission_spy):
        await admin_module.system_status(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_system_status")

    @pytest.mark.anyio
    async def test_feedback_requires_view_support(self, fake_supabase, permission_spy):
        await admin_module.list_feedback(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_support")

    @pytest.mark.anyio
    async def test_list_contacts_requires_view_support(self, fake_supabase, permission_spy):
        await admin_module.list_contact_messages(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_support")

    @pytest.mark.anyio
    async def test_update_contact_status_requires_manage_support(self, fake_supabase, permission_spy, recorded_audit_events):
        from app.routers.admin import ContactStatusInput

        await admin_module.update_contact_message_status(
            "msg-1", ContactStatusInput(status="beantwortet"), authorization="Bearer x"
        )
        assert permission_spy[-1] == ("Bearer x", "manage_support")

    @pytest.mark.anyio
    async def test_list_beta_applications_requires_view_support(self, fake_supabase, permission_spy, monkeypatch):
        monkeypatch.setattr(admin_module, "supabase_admin", fake_supabase)
        await admin_module.list_beta_applications(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_support")

    @pytest.mark.anyio
    async def test_analytics_requires_view_analytics(self, fake_supabase, permission_spy):
        await admin_module.analytics_growth(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_analytics")

    @pytest.mark.anyio
    async def test_list_content_requires_view_content(self, fake_supabase, permission_spy):
        await admin_module.list_content(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_content")

    @pytest.mark.anyio
    async def test_create_content_requires_manage_content(self, fake_supabase, permission_spy, recorded_audit_events):
        await admin_module.create_content(
            ContentInput(content_type="blog", title="Titel"), authorization="Bearer x"
        )
        assert permission_spy[-1] == ("Bearer x", "manage_content")

    @pytest.mark.anyio
    async def test_ai_usage_requires_view_ai_usage(self, fake_supabase, fake_internal_logging_supabase, permission_spy):
        await admin_module.ai_usage(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_ai_usage")

    @pytest.mark.anyio
    async def test_business_overview_requires_view_business(self, fake_supabase, fake_internal_logging_supabase, permission_spy):
        await admin_module.business_overview(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_business")

    @pytest.mark.anyio
    async def test_nutrition_overview_requires_view_nutrition_admin(self, fake_supabase, permission_spy):
        await admin_module.nutrition_overview(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_nutrition_admin")


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------


class TestListUsers:
    @pytest.mark.anyio
    async def test_never_selects_password_column(self, fake_supabase, permission_spy):
        await admin_module.list_users(authorization="Bearer x")
        select_calls = [entry for entry in fake_supabase.log if entry[0] == "vt_users" and entry[1] == "select"]
        assert select_calls, "expected a select() call against vt_users"
        for _, _, args, _ in select_calls:
            assert "password" not in args[0]

    @pytest.mark.anyio
    async def test_returns_empty_list_gracefully_on_db_failure(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"raise": True}
        result = await admin_module.list_users(authorization="Bearer x")
        assert result == {"items": [], "page": 1, "page_size": admin_module.DEFAULT_PAGE_SIZE, "total": 0}

    @pytest.mark.anyio
    async def test_enriches_rows_with_role_and_last_login(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com", "full_name": "User"}]}
        fake_supabase.store["vt_admin_roles"] = {"data": [{"email": "user@example.com", "role": "support"}]}
        fake_supabase.store["vt_login_events"] = {
            "data": [
                {"email": "user@example.com", "created_at": "2026-08-07T00:00:00Z"},
                {"email": "user@example.com", "created_at": "2026-08-01T00:00:00Z"},
            ]
        }
        result = await admin_module.list_users(authorization="Bearer x")
        assert result["items"][0]["role"] == "support"
        assert result["items"][0]["last_login_at"] == "2026-08-07T00:00:00Z"

    @pytest.mark.anyio
    async def test_role_and_last_login_are_null_when_absent(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com", "full_name": "User"}]}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        result = await admin_module.list_users(authorization="Bearer x")
        assert result["items"][0]["role"] is None
        assert result["items"][0]["last_login_at"] is None

    @pytest.mark.anyio
    async def test_shows_real_plan_not_derived_from_legacy_premium_when_plan_present(self, fake_supabase, permission_spy):
        """VitalTwin Plan System: a `pro` account must display as `pro`,
        never collapsed to the legacy `premium` boolean's true/false."""
        fake_supabase.store["vt_users"] = {
            "data": [{"email": "user@example.com", "full_name": "User", "premium": True, "plan": "pro", "suspended": False}]
        }
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        result = await admin_module.list_users(authorization="Bearer x")
        assert result["items"][0]["plan"] == "pro"
        assert result["items"][0]["status"] == "active"

    @pytest.mark.anyio
    async def test_falls_back_to_legacy_premium_boolean_when_plan_missing(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [{"email": "legacy@example.com", "full_name": "Legacy", "premium": True, "suspended": False}]
        }
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        result = await admin_module.list_users(authorization="Bearer x")
        assert result["items"][0]["plan"] == "premium"

    @pytest.mark.anyio
    async def test_status_is_deactivated_when_suspended(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [{"email": "user@example.com", "full_name": "User", "premium": False, "plan": "free", "suspended": True}]
        }
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        result = await admin_module.list_users(authorization="Bearer x")
        assert result["items"][0]["status"] == "deactivated"

    @pytest.mark.anyio
    async def test_status_is_deletion_requested_when_flagged(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [{"email": "user@example.com", "full_name": "User", "premium": False, "plan": "free", "suspended": False}]
        }
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {
            "data": [{"email": "user@example.com", "deletion_requested_at": "2026-08-08T00:00:00Z"}]
        }
        result = await admin_module.list_users(authorization="Bearer x")
        assert result["items"][0]["status"] == "deletion_requested"


class TestGetUserDetail:
    @pytest.mark.anyio
    async def test_404_when_user_not_found(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": []}
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.get_user_detail("nobody@example.com", authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_success_includes_consents_role_and_logins(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com", "full_name": "User"}]}
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": [{"role": "support"}]}
        fake_supabase.store["vt_login_events"] = {"data": [{"success": True, "created_at": "2024-01-01"}]}

        result = await admin_module.get_user_detail("user@example.com", authorization="Bearer x")
        assert result["user"]["email"] == "user@example.com"
        assert "password" not in result["user"]
        assert result["admin_role"] == "support"
        assert result["recent_logins"] == [{"success": True, "created_at": "2024-01-01"}]
        assert "consents" in result
        assert result["user"]["last_login_at"] == "2024-01-01"

    @pytest.mark.anyio
    async def test_last_login_at_is_null_when_no_successful_login_exists(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": [{"id": 1, "email": "user@example.com", "full_name": "User"}]}
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}

        result = await admin_module.get_user_detail("user@example.com", authorization="Bearer x")
        assert result["user"]["last_login_at"] is None
        assert result["user"]["id"] == 1

    @pytest.mark.anyio
    async def test_shows_real_plan_and_deletion_status(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [{"id": 1, "email": "user@example.com", "full_name": "User", "premium": True, "plan": "family", "suspended": False}]
        }
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": [{"deletion_requested_at": "2026-08-08T00:00:00Z"}]}
        fake_supabase.store["vt_stripe_subscriptions"] = {"data": []}

        result = await admin_module.get_user_detail("user@example.com", authorization="Bearer x")
        assert result["user"]["plan"] == "family"
        assert result["user"]["status"] == "deletion_requested"
        assert result["user"]["deletion_requested_at"] == "2026-08-08T00:00:00Z"

    @pytest.mark.anyio
    async def test_beta_access_true_when_paid_plan_but_no_stripe_subscription(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [{"id": 1, "email": "user@example.com", "full_name": "User", "premium": True, "plan": "premium", "suspended": False}]
        }
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        fake_supabase.store["vt_stripe_subscriptions"] = {"data": []}

        result = await admin_module.get_user_detail("user@example.com", authorization="Bearer x")
        assert result["user"]["beta_access"] is True

    @pytest.mark.anyio
    async def test_beta_access_false_when_a_real_stripe_subscription_exists(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [{"id": 1, "email": "user@example.com", "full_name": "User", "premium": True, "plan": "premium", "suspended": False}]
        }
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        fake_supabase.store["vt_stripe_subscriptions"] = {"data": [{"status": "active"}]}

        result = await admin_module.get_user_detail("user@example.com", authorization="Bearer x")
        assert result["user"]["beta_access"] is False

    @pytest.mark.anyio
    async def test_beta_access_false_for_free_plan(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [{"id": 1, "email": "user@example.com", "full_name": "User", "premium": False, "plan": "free", "suspended": False}]
        }
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        fake_supabase.store["vt_stripe_subscriptions"] = {"data": []}

        result = await admin_module.get_user_detail("user@example.com", authorization="Bearer x")
        assert result["user"]["beta_access"] is False


class TestGetUserDetailFamilyMembership:
    """Beta Tester Program hardening: admin must still see a preserved
    Family membership + whether it's currently locked, reusing family.py's
    own resolution (not a second engine)."""

    @pytest.mark.anyio
    async def test_none_when_never_in_a_family(self, monkeypatch, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": [{"id": 1, "email": "user@example.com", "full_name": "User", "plan": "free", "suspended": False}]}
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        fake_supabase.store["vt_stripe_subscriptions"] = {"data": []}
        monkeypatch.setattr(admin_module._family_router, "_get_open_membership", lambda user_id: None)

        result = await admin_module.get_user_detail("user@example.com", authorization="Bearer x")
        assert result["user"]["family_membership"] is None

    @pytest.mark.anyio
    async def test_shows_preserved_membership_with_locked_entitlement(self, monkeypatch, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": [{"id": 2, "email": "member@example.com", "full_name": "Member", "plan": "free", "suspended": False}]}
        fake_supabase.store["vt_consent_records"] = {"data": []}
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        fake_supabase.store["vt_login_events"] = {"data": []}
        fake_supabase.store["vt_user_profiles"] = {"data": []}
        fake_supabase.store["vt_stripe_subscriptions"] = {"data": []}
        monkeypatch.setattr(
            admin_module._family_router, "_get_open_membership",
            lambda user_id: {"family_id": 5, "role": "member", "status": "active"},
        )
        monkeypatch.setattr(admin_module._family_router, "_family_entitlement_active", lambda family_id: False)

        result = await admin_module.get_user_detail("member@example.com", authorization="Bearer x")
        assert result["user"]["family_membership"] == {
            "family_id": 5, "role": "member", "status": "active", "entitlement_active": False,
        }
    @pytest.mark.anyio
    async def test_list_only_returns_rows_with_a_deletion_request(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_user_profiles"] = {
            "data": [
                {"email": "a@example.com", "display_name": "A", "deletion_requested_at": "2026-08-01T00:00:00Z"},
                {"email": "b@example.com", "display_name": "B", "deletion_requested_at": None},
            ]
        }
        result = await admin_module.list_deletion_requests(authorization="Bearer x")
        assert [row["email"] for row in result["items"]] == ["a@example.com"]

    @pytest.mark.anyio
    async def test_list_requires_view_users_permission(self, fake_supabase, permission_spy):
        await admin_module.list_deletion_requests(authorization="Bearer x")
        assert permission_spy[-1][1] == "view_users"

    @pytest.mark.anyio
    async def test_complete_requires_manage_users_and_purges_and_audits(
        self, monkeypatch, permission_spy, recorded_audit_events
    ):
        from app.core import account_deletion

        recorded_calls: list[str] = []
        monkeypatch.setattr(
            account_deletion,
            "purge_all_user_data",
            lambda email: recorded_calls.append(email) or {"vt_users": 1, "vt_habits": 2},
        )
        monkeypatch.setattr(admin_module, "purge_all_user_data", account_deletion.purge_all_user_data)

        result = await admin_module.complete_deletion_request("user@example.com", authorization="Bearer x")

        assert permission_spy[-1][1] == "manage_users"
        assert recorded_calls == ["user@example.com"]
        assert result["deleted_rows"] == {"vt_users": 1, "vt_habits": 2}
        assert recorded_audit_events[-1]["action"] == "delete"
        assert recorded_audit_events[-1]["entity_type"] == "user_account"
        assert recorded_audit_events[-1]["entity_id"] == "user@example.com"


class TestDirectUserDeletion:
    """Admin-initiated hard delete (distinct from the GDPR self-service
    request/complete flow) — the real gap the founder reported: no button
    existed to remove a problematic user who never requested deletion
    themselves."""

    @pytest.mark.anyio
    async def test_deletes_user_and_records_audit_event(self, monkeypatch, fake_supabase, permission_spy, recorded_audit_events):
        from app.core import account_deletion

        fake_supabase.store["vt_admin_roles"] = {"data": []}
        monkeypatch.setattr(admin_module, "purge_all_user_data", lambda email: {"vt_users": 1, "vt_habits": 3})

        result = await admin_module.delete_user("spam@example.com", authorization="Bearer x")

        assert permission_spy[-1][1] == "manage_users"
        assert result["deleted_rows"] == {"vt_users": 1, "vt_habits": 3}
        assert recorded_audit_events[-1]["action"] == "delete"
        assert recorded_audit_events[-1]["entity_type"] == "user_account"
        assert recorded_audit_events[-1]["metadata"]["trigger"] == "admin_direct"

    @pytest.mark.anyio
    async def test_refuses_to_delete_a_super_admin(self, monkeypatch, fake_supabase, permission_spy):
        fake_supabase.store["vt_admin_roles"] = {"data": [{"role": "super_admin"}]}
        monkeypatch.setattr(admin_module, "purge_all_user_data", lambda email: {"vt_users": 1})

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.delete_user("founder@example.com", authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_404_when_user_does_not_exist(self, monkeypatch, fake_supabase, permission_spy):
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        monkeypatch.setattr(admin_module, "purge_all_user_data", lambda email: {"vt_users": 0})

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.delete_user("nobody@example.com", authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_refuses_when_actor_is_not_super_admin(self, monkeypatch, fake_supabase):
        """A real hard delete is higher-stakes than suspend/premium — even
        though `admin`/`support` also hold `manage_users`, only an actor
        whose OWN role is super_admin may perform it."""
        non_super_admin = AdminPrincipal(email="support@example.com", role="support")
        monkeypatch.setattr(admin_module, "require_admin_permission", lambda authorization, permission: non_super_admin)
        monkeypatch.setattr(admin_module, "purge_all_user_data", lambda email: {"vt_users": 1})

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.delete_user("user@example.com", authorization="Bearer x")
        assert exc_info.value.status_code == 403


class TestSuspendUnsuspend:
    @pytest.mark.anyio
    async def test_suspend_updates_row_and_records_audit_event(self, fake_supabase, permission_spy, recorded_audit_events):
        result = await admin_module.suspend_user(
            "user@example.com", SuspendInput(reason="Missbrauch"), authorization="Bearer x"
        )
        assert result["email"] == "user@example.com"
        updated = fake_supabase.store["vt_users"]["updated"][-1]
        assert updated["suspended"] is True
        assert updated["suspended_reason"] == "Missbrauch"

        assert recorded_audit_events[-1]["action"] == "update"
        assert recorded_audit_events[-1]["entity_type"] == "user_suspension"
        assert recorded_audit_events[-1]["entity_id"] == "user@example.com"
        assert recorded_audit_events[-1]["metadata"]["suspended"] is True

    @pytest.mark.anyio
    async def test_unsuspend_clears_suspension_and_records_audit_event(
        self, fake_supabase, permission_spy, recorded_audit_events
    ):
        await admin_module.unsuspend_user("user@example.com", authorization="Bearer x")
        updated = fake_supabase.store["vt_users"]["updated"][-1]
        assert updated["suspended"] is False
        assert updated["suspended_reason"] is None
        assert recorded_audit_events[-1]["metadata"]["suspended"] is False

    @pytest.mark.anyio
    async def test_suspend_raises_500_on_db_failure(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"raise": True}
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.suspend_user("user@example.com", SuspendInput(), authorization="Bearer x")
        assert exc_info.value.status_code == 500


class TestRoleManagement:
    def test_role_input_rejects_unknown_role(self):
        with pytest.raises(Exception):
            RoleInput(role="totally_made_up")

    def test_role_input_accepts_known_role(self):
        assert RoleInput(role="analyst").role == "analyst"

    @pytest.mark.anyio
    async def test_set_role_inserts_when_no_existing_row(self, fake_supabase, permission_spy, recorded_audit_events):
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        result = await admin_module.set_user_role(
            "user@example.com", RoleInput(role="support"), authorization="Bearer x"
        )
        assert result["role"] == "support"
        assert fake_supabase.store["vt_admin_roles"]["inserted"][-1]["email"] == "user@example.com"
        assert fake_supabase.store["vt_admin_roles"]["inserted"][-1]["granted_by"] == "admin@example.com"
        assert recorded_audit_events[-1]["entity_type"] == "admin_role"

    @pytest.mark.anyio
    async def test_set_role_updates_when_existing_row(self, fake_supabase, permission_spy, recorded_audit_events):
        fake_supabase.store["vt_admin_roles"] = {"data": [{"id": "existing-id"}]}
        await admin_module.set_user_role("user@example.com", RoleInput(role="moderator"), authorization="Bearer x")
        assert fake_supabase.store["vt_admin_roles"]["updated"][-1]["role"] == "moderator"
        assert "inserted" not in fake_supabase.store["vt_admin_roles"]

    @pytest.mark.anyio
    async def test_remove_role_deletes_and_records_audit_event(self, fake_supabase, permission_spy, recorded_audit_events):
        result = await admin_module.remove_user_role("user@example.com", authorization="Bearer x")
        assert result["email"] == "user@example.com"
        assert fake_supabase.store["vt_admin_roles"]["deleted"] is True
        assert recorded_audit_events[-1]["action"] == "delete"
        assert recorded_audit_events[-1]["entity_type"] == "admin_role"

    @pytest.mark.anyio
    async def test_refuses_to_demote_the_last_super_admin(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_admin_roles"] = {"data": [{"id": "existing-id", "role": "super_admin"}], "count": 0}
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.set_user_role("founder@example.com", RoleInput(role="admin"), authorization="Bearer x")
        assert exc_info.value.status_code == 409

    @pytest.mark.anyio
    async def test_allows_demoting_a_super_admin_when_another_remains(self, fake_supabase, permission_spy, recorded_audit_events):
        fake_supabase.store["vt_admin_roles"] = {"data": [{"id": "existing-id", "role": "super_admin"}], "count": 1}
        result = await admin_module.set_user_role("founder@example.com", RoleInput(role="admin"), authorization="Bearer x")
        assert result["role"] == "admin"

    @pytest.mark.anyio
    async def test_refuses_to_remove_the_last_super_admins_role(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_admin_roles"] = {"data": [{"role": "super_admin"}], "count": 0}
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.remove_user_role("founder@example.com", authorization="Bearer x")
        assert exc_info.value.status_code == 409


class TestPremiumManagement:
    @pytest.mark.anyio
    async def test_404_when_user_not_found(self, monkeypatch, fake_supabase, permission_spy):
        monkeypatch.setattr(admin_module, "set_premium_by_email", lambda email, premium: False)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.set_user_premium("nobody@example.com", PremiumInput(premium=True), authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_success_records_audit_event(self, monkeypatch, fake_supabase, permission_spy, recorded_audit_events):
        monkeypatch.setattr(admin_module, "set_premium_by_email", lambda email, premium: True)
        result = await admin_module.set_user_premium("user@example.com", PremiumInput(premium=True), authorization="Bearer x")
        assert result["premium"] is True
        assert recorded_audit_events[-1]["entity_type"] == "user_premium"
        assert recorded_audit_events[-1]["metadata"]["premium"] is True


class TestSetUserPlan:
    def test_rejects_unknown_plan_value(self):
        with pytest.raises(Exception):
            PlanChangeInput(plan="enterprise")

    @pytest.mark.anyio
    async def test_404_when_user_not_found(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": []}
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.set_user_plan("nobody@example.com", PlanChangeInput(plan="pro"), authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_success_calls_set_plan_by_email_and_records_audit_event(
        self, monkeypatch, fake_supabase, permission_spy, recorded_audit_events
    ):
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com"}]}
        calls: list[tuple] = []
        monkeypatch.setattr(admin_module, "set_plan_by_email", lambda email, plan: (calls.append((email, plan)), True)[1])

        result = await admin_module.set_user_plan("user@example.com", PlanChangeInput(plan="family"), authorization="Bearer x")

        assert calls == [("user@example.com", "family")]
        assert result["plan"] == "family"
        assert recorded_audit_events[-1]["entity_type"] == "user_plan"
        assert recorded_audit_events[-1]["metadata"] == {"plan": "family", "trigger": "admin_manual_override"}

    @pytest.mark.anyio
    async def test_surfaces_a_real_error_instead_of_a_false_success_when_the_write_fails(
        self, fake_supabase, permission_spy, monkeypatch
    ):
        """Regression test: the endpoint used to discard `set_plan_by_email`'s
        return value and always report success, even on a silent DB-write
        failure — a real bug found live (admin clicked "Pro", UI said
        success, but the plan never actually changed)."""
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com"}]}
        monkeypatch.setattr(admin_module, "set_plan_by_email", lambda email, plan: False)

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.set_user_plan("user@example.com", PlanChangeInput(plan="pro"), authorization="Bearer x")
        assert exc_info.value.status_code == 500


class TestBetaGrantEndpoint:
    def test_rejects_invalid_beta_plan_value(self):
        from app.routers.admin import BetaGrantInput
        with pytest.raises(Exception):
            BetaGrantInput(plan="free", days=30)

    def test_rejects_out_of_range_days(self):
        from app.routers.admin import BetaGrantInput
        with pytest.raises(Exception):
            BetaGrantInput(plan="pro", days=0)
        with pytest.raises(Exception):
            BetaGrantInput(plan="pro", days=400)

    @pytest.mark.anyio
    async def test_requires_manage_premium_permission(self, monkeypatch, fake_supabase, permission_spy):
        from app.routers.admin import BetaGrantInput
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com"}]}
        monkeypatch.setattr(admin_module, "grant_beta_by_email", lambda email, plan, days, granted_by: True)
        monkeypatch.setattr(admin_module, "get_beta_grant_by_email", lambda email: {"plan": "pro", "active": True})
        await admin_module.grant_beta_access("user@example.com", BetaGrantInput(plan="pro", days=90), authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "manage_premium")

    @pytest.mark.anyio
    async def test_404_when_user_not_found(self, fake_supabase, permission_spy):
        from app.routers.admin import BetaGrantInput
        fake_supabase.store["vt_users"] = {"data": []}
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.grant_beta_access("nobody@example.com", BetaGrantInput(plan="pro", days=90), authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_success_calls_grant_beta_by_email_and_records_audit_event(
        self, monkeypatch, fake_supabase, permission_spy, recorded_audit_events
    ):
        from app.routers.admin import BetaGrantInput
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com"}]}
        calls: list[tuple] = []
        monkeypatch.setattr(
            admin_module, "grant_beta_by_email",
            lambda email, plan, days, granted_by: (calls.append((email, plan, days, granted_by)), True)[1],
        )
        monkeypatch.setattr(admin_module, "get_beta_grant_by_email", lambda email: {"plan": "pro", "active": True})

        result = await admin_module.grant_beta_access("user@example.com", BetaGrantInput(plan="pro", days=90), authorization="Bearer x")

        assert calls == [("user@example.com", "pro", 90, "admin@example.com")]
        assert result["beta_grant"]["plan"] == "pro"
        assert recorded_audit_events[-1]["entity_type"] == "user_beta_grant"
        assert recorded_audit_events[-1]["metadata"]["trigger"] == "admin_beta_grant"

    @pytest.mark.anyio
    async def test_surfaces_error_when_grant_write_fails(self, fake_supabase, permission_spy, monkeypatch):
        from app.routers.admin import BetaGrantInput
        fake_supabase.store["vt_users"] = {"data": [{"email": "user@example.com"}]}
        monkeypatch.setattr(admin_module, "grant_beta_by_email", lambda email, plan, days, granted_by: False)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.grant_beta_access("user@example.com", BetaGrantInput(plan="family", days=30), authorization="Bearer x")
        assert exc_info.value.status_code == 500


class TestBetaExtendEndpoint:
    @pytest.mark.anyio
    async def test_requires_manage_premium_permission(self, monkeypatch, permission_spy):
        from app.routers.admin import BetaExtendInput
        monkeypatch.setattr(admin_module, "extend_beta_by_email", lambda email, days, granted_by: {"plan": "pro", "expires_at": "x"})
        await admin_module.extend_beta_access("user@example.com", BetaExtendInput(days=30), authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "manage_premium")

    @pytest.mark.anyio
    async def test_404_when_nothing_to_extend(self, monkeypatch, permission_spy):
        from app.routers.admin import BetaExtendInput
        monkeypatch.setattr(admin_module, "extend_beta_by_email", lambda email, days, granted_by: None)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.extend_beta_access("user@example.com", BetaExtendInput(days=30), authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_success_records_audit_event(self, monkeypatch, permission_spy, recorded_audit_events):
        from app.routers.admin import BetaExtendInput
        monkeypatch.setattr(
            admin_module, "extend_beta_by_email",
            lambda email, days, granted_by: {"plan": "family", "expires_at": "2026-12-01T00:00:00+00:00"},
        )
        result = await admin_module.extend_beta_access("user@example.com", BetaExtendInput(days=30), authorization="Bearer x")
        assert result["beta_grant"]["plan"] == "family"
        assert recorded_audit_events[-1]["entity_type"] == "user_beta_grant"
        assert recorded_audit_events[-1]["metadata"]["trigger"] == "admin_beta_extend"


class TestBetaRevokeEndpoint:
    @pytest.mark.anyio
    async def test_requires_manage_premium_permission(self, monkeypatch, permission_spy):
        monkeypatch.setattr(admin_module, "revoke_beta_by_email", lambda email: {"plan": "pro"})
        await admin_module.revoke_beta_access("user@example.com", authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "manage_premium")

    @pytest.mark.anyio
    async def test_404_when_nothing_active(self, monkeypatch, permission_spy):
        monkeypatch.setattr(admin_module, "revoke_beta_by_email", lambda email: None)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.revoke_beta_access("user@example.com", authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_success_records_audit_event_with_previous_plan(self, monkeypatch, permission_spy, recorded_audit_events):
        monkeypatch.setattr(admin_module, "revoke_beta_by_email", lambda email: {"plan": "family"})
        result = await admin_module.revoke_beta_access("user@example.com", authorization="Bearer x")
        assert result["email"] == "user@example.com"
        assert recorded_audit_events[-1]["metadata"] == {"revoked_plan": "family", "trigger": "admin_beta_revoke"}


class TestListBetaTesters:
    @pytest.mark.anyio
    async def test_requires_view_users_permission(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": []}
        await admin_module.list_beta_testers(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_users")

    @pytest.mark.anyio
    async def test_classifies_active_expiring_and_expired(self, fake_supabase, permission_spy):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        fake_supabase.store["vt_users"] = {
            "data": [
                {
                    "email": "active@example.com", "full_name": "Active Tester", "plan": "free",
                    "beta_plan": "pro", "beta_started_at": now.isoformat(),
                    "beta_expires_at": (now + timedelta(days=60)).isoformat(), "beta_granted_by": "admin@example.com",
                },
                {
                    "email": "soon@example.com", "full_name": "Expiring Soon", "plan": "free",
                    "beta_plan": "family", "beta_started_at": now.isoformat(),
                    "beta_expires_at": (now + timedelta(days=3)).isoformat(), "beta_granted_by": "admin@example.com",
                },
                {
                    "email": "expired@example.com", "full_name": "Expired Tester", "plan": "free",
                    "beta_plan": "premium", "beta_started_at": (now - timedelta(days=100)).isoformat(),
                    "beta_expires_at": (now - timedelta(days=10)).isoformat(), "beta_granted_by": "admin@example.com",
                },
            ]
        }
        result = await admin_module.list_beta_testers(authorization="Bearer x")
        statuses = {t["email"]: t["status"] for t in result["testers"]}
        assert statuses["active@example.com"] == "active"
        assert statuses["soon@example.com"] == "expiring_soon"
        assert statuses["expired@example.com"] == "expired"
        assert result["summary"]["total"] == 3
        assert result["summary"]["pro_beta"] == 1
        assert result["summary"]["family_beta"] == 1
        assert result["summary"]["expired"] == 1

    @pytest.mark.anyio
    async def test_never_exposes_wellness_data_fields(self, fake_supabase, permission_spy):
        """DSGVO/privacy boundary: this overview must only ever contain
        access-management metadata — never health/wellness data, even if
        a caller accidentally widened the select elsewhere."""
        fake_supabase.store["vt_users"] = {
            "data": [
                {
                    "email": "x@example.com", "full_name": "X", "plan": "free", "beta_plan": "pro",
                    "beta_started_at": "2026-01-01T00:00:00+00:00", "beta_expires_at": "2026-04-01T00:00:00+00:00",
                    "beta_granted_by": "admin@example.com",
                }
            ]
        }
        result = await admin_module.list_beta_testers(authorization="Bearer x")
        allowed_keys = {"email", "full_name", "real_plan", "beta_plan", "beta_started_at", "beta_expires_at", "beta_granted_by", "status", "remaining_days"}
        assert set(result["testers"][0].keys()) <= allowed_keys


class TestListBetaDiscountGrants:
    @pytest.mark.anyio
    async def test_requires_view_users_permission(self, monkeypatch, permission_spy, fake_supabase):
        monkeypatch.setattr(admin_module, "list_discount_grants", lambda: [])
        await admin_module.list_beta_discount_grants(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_users")

    @pytest.mark.anyio
    async def test_returns_summary_and_grants(self, monkeypatch, permission_spy, fake_supabase):
        grants = [
            {"email": "first@example.com", "slot_number": 1, "status": "granted", "is_test_grant": False},
            {"email": "second@example.com", "slot_number": 2, "status": "applied", "is_test_grant": False},
        ]
        monkeypatch.setattr(admin_module, "list_discount_grants", lambda: grants)
        result = await admin_module.list_beta_discount_grants(authorization="Bearer x")
        assert result["total_slots"] == 20
        assert result["claimed_slots"] == 2
        assert result["remaining_slots"] == 18
        assert result["grants"] == grants
        assert result["test_grants"] == []

    @pytest.mark.anyio
    async def test_remaining_slots_never_negative(self, monkeypatch, permission_spy, fake_supabase):
        grants = [{"email": f"real{i}@example.com", "slot_number": i, "is_test_grant": False} for i in range(1, 21)]
        monkeypatch.setattr(admin_module, "list_discount_grants", lambda: grants)
        result = await admin_module.list_beta_discount_grants(authorization="Bearer x")
        assert result["remaining_slots"] == 0

    @pytest.mark.anyio
    async def test_qa_test_grant_excluded_from_real_counts_and_listed_separately(self, monkeypatch, permission_spy, fake_supabase):
        grants = [
            {"email": "qa-test-screenshot-demo@example.com", "slot_number": 1, "status": "granted", "is_test_grant": True},
            {"email": "real-tester@example.com", "slot_number": 2, "status": "granted", "is_test_grant": False},
        ]
        monkeypatch.setattr(admin_module, "list_discount_grants", lambda: grants)
        result = await admin_module.list_beta_discount_grants(authorization="Bearer x")
        assert result["claimed_slots"] == 1
        assert result["remaining_slots"] == 19
        assert [g["email"] for g in result["grants"]] == ["real-tester@example.com"]
        assert [g["email"] for g in result["test_grants"]] == ["qa-test-screenshot-demo@example.com"]

    @pytest.mark.anyio
    async def test_classification_survives_a_deleted_vt_users_row(self, monkeypatch, permission_spy, fake_supabase):
        """The exact regression migration 046 fixes: the grant's own
        is_test_grant column must be authoritative even when vt_users has
        NO row at all for that email (post-deletion) -- proven by never
        populating fake_supabase's vt_users store in this test."""
        grants = [
            {"email": "qa-test-screenshot-demo@example.com", "slot_number": 1, "status": "granted", "is_test_grant": True},
            {"email": "real-tester@example.com", "slot_number": 2, "status": "granted", "is_test_grant": False},
        ]
        monkeypatch.setattr(admin_module, "list_discount_grants", lambda: grants)
        result = await admin_module.list_beta_discount_grants(authorization="Bearer x")
        assert result["claimed_slots"] == 1
        assert [g["email"] for g in result["test_grants"]] == ["qa-test-screenshot-demo@example.com"]


class TestQACleanup:
    @pytest.mark.anyio
    async def test_preview_requires_super_admin(self, monkeypatch, fake_supabase):
        non_super_admin = AdminPrincipal(email="support@example.com", role="support")
        monkeypatch.setattr(admin_module, "require_admin_permission", lambda authorization, permission: non_super_admin)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.preview_qa_cleanup(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_preview_only_matches_the_strict_qa_pattern(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {
            "data": [
                {"email": "qa-test-plan-free-20260808@vitaltwin.de", "full_name": "QA TEST ACCOUNT - PLAN FREE", "created_at": "2026-08-08"},
                {"email": "real.customer@example.com", "full_name": "Real Customer", "created_at": "2026-01-01"},
                # email prefix matches but name marker is missing -> must NOT match (double-safety)
                {"email": "qa-test-suspicious@vitaltwin.de", "full_name": "Not a QA marker", "created_at": "2026-08-08"},
                # name marker present but email prefix missing -> must NOT match either
                {"email": "someone@example.com", "full_name": "QA TEST ACCOUNT - fake", "created_at": "2026-08-08"},
            ]
        }
        result = await admin_module.preview_qa_cleanup(authorization="Bearer x")
        assert result["count"] == 1
        assert result["items"][0]["email"] == "qa-test-plan-free-20260808@vitaltwin.de"

    @pytest.mark.anyio
    async def test_execute_requires_super_admin(self, monkeypatch, fake_supabase):
        non_super_admin = AdminPrincipal(email="support@example.com", role="support")
        monkeypatch.setattr(admin_module, "require_admin_permission", lambda authorization, permission: non_super_admin)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.execute_qa_cleanup(QACleanupExecuteInput(confirm=True), authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_execute_requires_explicit_confirm(self, fake_supabase, permission_spy):
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.execute_qa_cleanup(QACleanupExecuteInput(confirm=False), authorization="Bearer x")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_execute_deletes_only_matching_accounts_and_reports_summary(
        self, monkeypatch, fake_supabase, permission_spy, recorded_audit_events
    ):
        fake_supabase.store["vt_users"] = {
            "data": [
                {"email": "qa-test-a@vitaltwin.de", "full_name": "QA TEST ACCOUNT - A"},
                {"email": "qa-test-b@vitaltwin.de", "full_name": "QA TEST ACCOUNT - B"},
                {"email": "real.customer@example.com", "full_name": "Real Customer"},
            ]
        }
        fake_supabase.store["vt_admin_roles"] = {"data": []}
        purge_calls: list[str] = []

        def _fake_purge(email):
            purge_calls.append(email)
            if email == "qa-test-b@vitaltwin.de":
                raise RuntimeError("boom")
            return {"vt_users": 1}

        monkeypatch.setattr(admin_module, "purge_all_user_data", _fake_purge)

        result = await admin_module.execute_qa_cleanup(QACleanupExecuteInput(confirm=True), authorization="Bearer x")

        assert set(purge_calls) == {"qa-test-a@vitaltwin.de", "qa-test-b@vitaltwin.de"}
        assert "real.customer@example.com" not in purge_calls
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert result["message"] == "1 QA-Testaccounts erfolgreich bereinigt, 1 fehlgeschlagen"
        assert recorded_audit_events[-1]["entity_type"] == "qa_cleanup"

    @pytest.mark.anyio
    async def test_execute_never_touches_a_normal_user_even_with_matching_email_prefix_alone(
        self, monkeypatch, fake_supabase, permission_spy
    ):
        """The double-safety rule: email prefix ALONE is not enough."""
        fake_supabase.store["vt_users"] = {
            "data": [{"email": "qa-test-looks-like-a-real-user@vitaltwin.de", "full_name": "Kein QA-Marker im Namen"}]
        }
        purge_calls: list[str] = []
        monkeypatch.setattr(admin_module, "purge_all_user_data", lambda email: purge_calls.append(email) or {"vt_users": 1})

        result = await admin_module.execute_qa_cleanup(QACleanupExecuteInput(confirm=True), authorization="Bearer x")

        assert purge_calls == []
        assert result["attempted"] == 0

    @pytest.mark.anyio
    async def test_execute_skips_accounts_with_an_admin_role(self, monkeypatch, fake_supabase, permission_spy):
        fake_supabase.store["vt_users"] = {"data": [{"email": "qa-test-admin@vitaltwin.de", "full_name": "QA TEST ACCOUNT - admin"}]}
        fake_supabase.store["vt_admin_roles"] = {"data": [{"email": "qa-test-admin@vitaltwin.de"}]}
        purge_calls: list[str] = []
        monkeypatch.setattr(admin_module, "purge_all_user_data", lambda email: purge_calls.append(email) or {"vt_users": 1})

        result = await admin_module.execute_qa_cleanup(QACleanupExecuteInput(confirm=True), authorization="Bearer x")

        assert purge_calls == []
        assert result["succeeded"] == 0
        assert result["results"][0]["success"] is False


# ---------------------------------------------------------------------------
# Security Center
# ---------------------------------------------------------------------------


class TestSecurityCenter:
    @pytest.mark.anyio
    async def test_permission_matrix_matches_role_permissions(self, fake_supabase, permission_spy):
        from app.core.admin_rbac import ROLE_PERMISSIONS

        result = await admin_module.get_permission_matrix(authorization="Bearer x")
        assert set(result["roles"].keys()) == set(ROLE_PERMISSIONS.keys())
        assert result["roles"]["editor"] == sorted(ROLE_PERMISSIONS["editor"])


class TestSupportContactsAndBetaApplications:
    @pytest.mark.anyio
    async def test_list_contacts_returns_items(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_contact_messages"] = {
            "data": [{"id": "m1", "full_name": "A", "email": "a@example.com", "status": "new"}],
            "count": 1,
        }
        result = await admin_module.list_contact_messages(authorization="Bearer x")
        assert result["items"][0]["id"] == "m1"
        assert result["total"] == 1

    @pytest.mark.anyio
    async def test_list_contacts_filters_by_status(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_contact_messages"] = {"data": [], "count": 0}
        await admin_module.list_contact_messages(status="beantwortet", authorization="Bearer x")
        assert ("vt_contact_messages", "eq", ("status", "beantwortet"), {}) in fake_supabase.log

    @pytest.mark.anyio
    async def test_update_contact_status_updates_row_and_records_audit_event(
        self, fake_supabase, permission_spy, recorded_audit_events
    ):
        from app.routers.admin import ContactStatusInput

        result = await admin_module.update_contact_message_status(
            "msg-1", ContactStatusInput(status="archiviert"), authorization="Bearer x"
        )
        assert result["status"] == "archiviert"
        updated = fake_supabase.store["vt_contact_messages"]["updated"][-1]
        assert updated["status"] == "archiviert"
        assert recorded_audit_events[-1]["entity_type"] == "contact_message"
        assert recorded_audit_events[-1]["entity_id"] == "msg-1"

    @pytest.mark.anyio
    async def test_update_contact_status_rejects_invalid_status(self):
        from pydantic import ValidationError

        from app.routers.admin import ContactStatusInput

        with pytest.raises(ValidationError):
            ContactStatusInput(status="unbekannt")

    @pytest.mark.anyio
    async def test_list_beta_applications_returns_items_with_status(self, fake_supabase, permission_spy, monkeypatch):
        fake_supabase.store["vt_beta_applications"] = {
            "data": [{"email": "beta@example.com", "full_name": "Beta Tester", "motivation": "Test", "status": "pending"}],
            "count": 1,
        }
        monkeypatch.setattr(admin_module, "supabase_admin", fake_supabase)
        result = await admin_module.list_beta_applications(authorization="Bearer x")
        assert result["items"][0]["email"] == "beta@example.com"
        assert result["items"][0]["status"] == "pending"
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# Content Management
# ---------------------------------------------------------------------------


class TestContentManagement:
    def test_content_input_rejects_unknown_content_type(self):
        with pytest.raises(Exception):
            ContentInput(content_type="not_a_real_type", title="x")

    def test_content_input_rejects_unknown_status(self):
        with pytest.raises(Exception):
            ContentInput(content_type="blog", title="x", status="not_a_real_status")

    @pytest.mark.anyio
    async def test_create_sets_created_by_and_published_at(self, fake_supabase, permission_spy, recorded_audit_events):
        fake_supabase.store["vt_content_items"] = {"insert_result": [{"id": "1"}]}
        await admin_module.create_content(
            ContentInput(content_type="blog", title="Titel", status="published"), authorization="Bearer x"
        )
        inserted = fake_supabase.store["vt_content_items"]["inserted"][-1]
        assert inserted["created_by"] == "admin@example.com"
        assert inserted["published_at"] is not None
        assert recorded_audit_events[-1]["action"] == "create"

    @pytest.mark.anyio
    async def test_create_does_not_set_published_at_for_draft(self, fake_supabase, permission_spy, recorded_audit_events):
        fake_supabase.store["vt_content_items"] = {"insert_result": [{"id": "1"}]}
        await admin_module.create_content(
            ContentInput(content_type="blog", title="Titel", status="draft"), authorization="Bearer x"
        )
        inserted = fake_supabase.store["vt_content_items"]["inserted"][-1]
        assert "published_at" not in inserted

    @pytest.mark.anyio
    async def test_delete_records_audit_event(self, fake_supabase, permission_spy, recorded_audit_events):
        await admin_module.delete_content("content-id-1", authorization="Bearer x")
        assert fake_supabase.store["vt_content_items"]["deleted"] is True
        assert recorded_audit_events[-1]["action"] == "delete"
        assert recorded_audit_events[-1]["entity_id"] == "content-id-1"


# ---------------------------------------------------------------------------
# Honest "not implemented" notes — the core "Ehrlichkeit" guarantee
# ---------------------------------------------------------------------------


class TestHonestyNotes:
    @pytest.mark.anyio
    async def test_nutrition_overview_is_an_honest_stub(self, fake_supabase, permission_spy):
        result = await admin_module.nutrition_overview(authorization="Bearer x")
        assert result["available"] is False
        assert "note" in result and len(result["note"]) > 0

    @pytest.mark.anyio
    async def test_nutrition_overview_reports_real_stats_once_data_exists(self, fake_supabase, permission_spy):
        fake_supabase.store["vt_cgm_readings"] = {
            "data": [
                {"email": "a@example.com", "glucose_value": 110, "reading_at": "2026-07-20T08:00:00+00:00"},
                {"email": "a@example.com", "glucose_value": 115, "reading_at": "2026-07-20T08:15:00+00:00"},
                {"email": "b@example.com", "glucose_value": 120, "reading_at": "2026-07-20T09:00:00+00:00"},
            ]
        }
        fake_supabase.store["vt_nutrition_entries"] = {
            "data": [
                {"email": "a@example.com", "meal_name": "Haferflocken", "carbs": 40, "logged_at": "2026-07-20T08:00:00+00:00"},
            ]
        }
        result = await admin_module.nutrition_overview(authorization="Bearer x")
        assert result["available"] is True
        assert result["cgm"]["total_readings"] == 3
        assert result["cgm"]["unique_users"] == 2
        assert result["nutrition"]["total_entries"] == 1
        assert result["nutrition"]["unique_users"] == 1
        assert result["nutrition"]["last_entries"][0]["meal_name"] == "Haferflocken"

    @pytest.mark.anyio
    async def test_dashboard_reports_revenue_and_error_tracking_notes(self, fake_supabase, fake_internal_logging_supabase, permission_spy):
        result = await admin_module.admin_dashboard(authorization="Bearer x")
        assert "revenue_note" in result
        assert "error_tracking_note" in result

    @pytest.mark.anyio
    async def test_ai_usage_reports_token_and_prompt_versioning_notes(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy
    ):
        result = await admin_module.ai_usage(authorization="Bearer x")
        assert "token_usage_note" in result
        assert "prompt_versions_note" in result

    @pytest.mark.anyio
    async def test_business_overview_reports_revenue_affiliate_and_coupon_notes(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy
    ):
        result = await admin_module.business_overview(authorization="Bearer x")
        assert "revenue_note" in result
        assert "affiliate_note" in result
        assert "coupons_note" in result

    @pytest.mark.anyio
    async def test_system_status_reports_cron_queue_and_health_notes(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy
    ):
        result = await admin_module.system_status(authorization="Bearer x")
        assert "note" in result["cron_jobs"]
        assert "note" in result["queues"]
        assert "note" in result["health_connect"]
        assert "note" in result["apple_health"]

    @pytest.mark.anyio
    async def test_system_status_reports_honest_no_release_no_backup_when_empty(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy
    ):
        result = await admin_module.system_status(authorization="Bearer x")
        assert "note" in result["release"]
        assert "note" in result["backup"]
        assert result["error_events_7d"]["total"] == 0

    @pytest.mark.anyio
    async def test_ai_usage_reports_real_token_and_cost_summary(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy
    ):
        fake_internal_logging_supabase.ai_usage.store["vt_ai_usage_events"] = {
            "data": [
                {"status": "success", "total_tokens": 100, "cost_usd": None, "cost_note": "x", "latency_ms": 200},
            ]
        }
        result = await admin_module.ai_usage(authorization="Bearer x")
        assert result["usage_today"]["requests"] == 1
        assert result["usage_30d"]["requests"] == 1


class TestReleasesAndBackups:
    @pytest.mark.anyio
    async def test_create_release_requires_manage_founder_os(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy, recorded_audit_events
    ):
        fake_internal_logging_supabase.release.store["vt_founder_releases"] = {
            "insert_result": [{"id": 1, "version": "1.2.0", "build_status": "unbekannt"}]
        }
        result = await admin_module.create_release(
            ReleaseInput(version="1.2.0", description="Test-Release"), authorization="Bearer x"
        )
        assert result["version"] == "1.2.0"
        assert ("Bearer x", "manage_founder_os") in permission_spy
        assert recorded_audit_events[0]["entity_type"] == "release"

    @pytest.mark.anyio
    async def test_get_releases_empty_by_default(self, fake_supabase, fake_internal_logging_supabase, permission_spy):
        result = await admin_module.get_releases(authorization="Bearer x")
        assert result == {"items": [], "latest": None}

    @pytest.mark.anyio
    async def test_get_releases_returns_latest_after_create(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy
    ):
        fake_internal_logging_supabase.release.store["vt_founder_releases"] = {
            "insert_result": [{"id": 1, "version": "1.0.0", "build_status": "unbekannt"}],
            "data": [{"id": 1, "version": "1.0.0", "build_status": "unbekannt"}],
        }
        await admin_module.create_release(ReleaseInput(version="1.0.0"), authorization="Bearer x")
        result = await admin_module.get_releases(authorization="Bearer x")
        assert result["latest"]["version"] == "1.0.0"

    @pytest.mark.anyio
    async def test_create_backup_requires_manage_founder_os(
        self, fake_supabase, fake_internal_logging_supabase, permission_spy, recorded_audit_events
    ):
        fake_internal_logging_supabase.backup.store["vt_founder_backup_status"] = {
            "insert_result": [{"id": 1, "status": "erfolgreich"}]
        }
        result = await admin_module.create_backup(BackupInput(status="erfolgreich"), authorization="Bearer x")
        assert result["status"] == "erfolgreich"
        assert ("Bearer x", "manage_founder_os") in permission_spy

    @pytest.mark.anyio
    async def test_create_backup_rejects_invalid_status(self):
        with pytest.raises(Exception):
            BackupInput(status="not_a_real_status")

    @pytest.mark.anyio
    async def test_get_backups_empty_by_default(self, fake_supabase, fake_internal_logging_supabase, permission_spy):
        result = await admin_module.get_backups(authorization="Bearer x")
        assert result == {"items": [], "latest": None}


class TestReleaseAndBackupWebhooks:
    """Shared-secret CI/CD/backup-job webhooks — no admin JWT involved."""

    @staticmethod
    def _fake_request():
        return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    @pytest.mark.anyio
    async def test_release_webhook_disabled_without_secret_configured(self, monkeypatch, fake_internal_logging_supabase):
        monkeypatch.delenv("RELEASE_WEBHOOK_SECRET", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.create_release_webhook(
                ReleaseInput(version="1.0.0"), self._fake_request(), x_webhook_secret="anything"
            )
        assert exc_info.value.status_code == 503

    @pytest.mark.anyio
    async def test_release_webhook_rejects_wrong_secret(self, monkeypatch, fake_internal_logging_supabase):
        monkeypatch.setenv("RELEASE_WEBHOOK_SECRET", "correct-secret")
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.create_release_webhook(
                ReleaseInput(version="1.0.0"), self._fake_request(), x_webhook_secret="wrong-secret"
            )
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_release_webhook_accepts_correct_secret(
        self, monkeypatch, fake_internal_logging_supabase, recorded_audit_events
    ):
        monkeypatch.setenv("RELEASE_WEBHOOK_SECRET", "correct-secret")
        monkeypatch.setattr(admin_module, "record_audit_event", lambda **kwargs: recorded_audit_events.append(kwargs))
        fake_internal_logging_supabase.release.store["vt_founder_releases"] = {
            "insert_result": [{"id": 1, "version": "2.0.0", "build_status": "erfolgreich"}]
        }
        result = await admin_module.create_release_webhook(
            ReleaseInput(version="2.0.0", build_status="erfolgreich"),
            self._fake_request(),
            x_webhook_secret="correct-secret",
        )
        assert result["version"] == "2.0.0"
        assert recorded_audit_events[-1]["email"] == "ci_cd_pipeline"

    @pytest.mark.anyio
    async def test_backup_webhook_disabled_without_secret_configured(self, monkeypatch, fake_internal_logging_supabase):
        monkeypatch.delenv("BACKUP_WEBHOOK_SECRET", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            await admin_module.create_backup_webhook(
                BackupInput(status="erfolgreich"), self._fake_request(), x_webhook_secret="anything"
            )
        assert exc_info.value.status_code == 503

    @pytest.mark.anyio
    async def test_backup_webhook_accepts_correct_secret(self, monkeypatch, fake_internal_logging_supabase):
        monkeypatch.setenv("BACKUP_WEBHOOK_SECRET", "correct-secret")
        monkeypatch.setattr(admin_module, "record_audit_event", lambda **kwargs: None)
        fake_internal_logging_supabase.backup.store["vt_founder_backup_status"] = {
            "insert_result": [{"id": 1, "status": "erfolgreich"}]
        }
        result = await admin_module.create_backup_webhook(
            BackupInput(status="erfolgreich"), self._fake_request(), x_webhook_secret="correct-secret"
        )
        assert result["status"] == "erfolgreich"
