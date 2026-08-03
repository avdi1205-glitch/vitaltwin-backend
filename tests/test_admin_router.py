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
from app.routers.admin import BackupInput, ContentInput, PremiumInput, ReleaseInput, RoleInput, SuspendInput


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

    def gte(self, *args, **kwargs):
        self._record("gte", *args, **kwargs)
        return self

    def or_(self, *args, **kwargs):
        self._record("or_", *args, **kwargs)
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


class TestDeletionRequests:
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
