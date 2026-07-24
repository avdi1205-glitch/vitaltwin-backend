"""Tests for the Platform Foundation & Integration Architecture (Release 0):
`core/integrations.py` (pure registry, no mocking needed), the new
`/api/admin/integrations` + `/api/admin/feature-flags*` endpoints, and the
in-app notifications router — the one genuinely implemented notification
channel."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core import integrations as integrations_module
from app.routers import admin as admin_module
from app.routers import notifications as notifications_module
from tests.test_admin_router import fake_supabase, permission_spy, recorded_audit_events, super_admin_principal  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TestIntegrationRegistry:
    def test_health_connectors_are_all_not_implemented(self):
        connectors = integrations_module.get_health_connectors()
        assert len(connectors) == 9
        assert all(c.status == "not_implemented" and c.implemented is False for c in connectors)

    def test_stripe_reflects_real_env_var(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
        providers = {p.id: p for p in integrations_module.get_payment_providers()}
        assert providers["stripe"].status == "configured"
        assert providers["paypal"].status == "not_implemented"

    def test_stripe_not_configured_without_env_var(self, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        providers = {p.id: p for p in integrations_module.get_payment_providers()}
        assert providers["stripe"].status == "not_configured"

    def test_ai_providers_only_openai_implemented(self):
        providers = {p.id: p for p in integrations_module.get_ai_providers()}
        assert providers["openai"].implemented is True
        assert providers["anthropic"].implemented is False
        assert providers["gemini"].implemented is False

    def test_affiliate_networks_all_not_implemented(self):
        networks = integrations_module.get_affiliate_networks()
        assert len(networks) == 6
        assert all(n.status == "not_implemented" for n in networks)

    def test_full_report_has_all_categories(self):
        report = integrations_module.get_full_integration_report()
        assert set(report.keys()) == {
            "platforms",
            "health_connectors",
            "payment_providers",
            "affiliate_networks",
            "auth_providers",
            "ai_providers",
            "notification_channels",
        }


class TestIntegrationsAdminEndpoint:
    @pytest.mark.anyio
    async def test_requires_view_integrations_permission(self, permission_spy):
        await admin_module.list_integrations(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_integrations")

    @pytest.mark.anyio
    async def test_feature_flags_list_requires_view_integrations(self, fake_supabase, permission_spy):
        await admin_module.list_feature_flags(authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "view_integrations")

    @pytest.mark.anyio
    async def test_feature_flag_upsert_requires_manage_permission(self, fake_supabase, permission_spy):
        data = admin_module.FeatureFlagInput(enabled=True, description="test flag")
        await admin_module.upsert_feature_flag("some_flag", data, authorization="Bearer x")
        assert permission_spy[-1] == ("Bearer x", "manage_feature_flags")
        assert fake_supabase.store["vt_feature_flags"]["upserted"][0]["key"] == "some_flag"
        assert fake_supabase.store["vt_feature_flags"]["upserted"][0]["enabled"] is True


class TestNotificationsRouter:
    @pytest.mark.anyio
    async def test_list_notifications_requires_login(self, monkeypatch):
        def _raise(auth):
            raise HTTPException(status_code=401, detail="Nicht eingeloggt")

        monkeypatch.setattr(notifications_module, "require_email", _raise)
        with pytest.raises(HTTPException) as exc_info:
            await notifications_module.list_notifications(authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_list_notifications_returns_unread_count(self, monkeypatch):
        monkeypatch.setattr(notifications_module, "require_email", lambda auth: "user@example.com")

        class _FakeResult:
            data = [
                {"id": "1", "title": "a", "body": "b", "read": False, "created_at": "2026-01-01"},
                {"id": "2", "title": "c", "body": "d", "read": True, "created_at": "2026-01-02"},
            ]

        class _FakeQuery:
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def order(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def execute(self): return _FakeResult()

        class _FakeSupabase:
            def table(self, name): return _FakeQuery()

        monkeypatch.setattr(notifications_module, "supabase", _FakeSupabase())
        result = await notifications_module.list_notifications(authorization="Bearer x")
        assert result["unread_count"] == 1
        assert len(result["items"]) == 2

    def test_create_notification_is_best_effort(self, monkeypatch):
        class _FakeQuery:
            def insert(self, payload): return self
            def execute(self): raise RuntimeError("boom")

        class _FakeSupabase:
            def table(self, name): return _FakeQuery()

        monkeypatch.setattr(notifications_module, "supabase", _FakeSupabase())
        # Must not raise even if the underlying insert fails.
        assert notifications_module.create_notification(email="user@example.com", title="t", body="b") is False
