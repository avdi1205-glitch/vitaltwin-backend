"""Unit tests for `app.core.admin_rbac` — the RBAC foundation of the Admin
Control Center. Mocks Supabase and `core.auth.require_email` — no real
network/database access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import admin_rbac as rbac_module
from app.core.admin_rbac import (
    ROLE_PERMISSIONS,
    AdminPrincipal,
    get_admin_role,
    require_admin,
    require_admin_permission,
    role_has_permission,
)


class _RecordingQuery:
    def __init__(self, data=None, raise_error: bool = False):
        self._data = data if data is not None else []
        self._raise_error = raise_error

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        if self._raise_error:
            raise RuntimeError("boom")
        return SimpleNamespace(data=self._data)


class _FakeSupabase:
    def __init__(self, data=None, raise_error: bool = False):
        self._data = data
        self._raise_error = raise_error

    def table(self, name):
        return _RecordingQuery(self._data, self._raise_error)


class TestPermissionMatrix:
    def test_all_seven_roles_are_defined(self):
        assert set(ROLE_PERMISSIONS.keys()) == {
            "super_admin",
            "admin",
            "support",
            "moderator",
            "editor",
            "analyst",
            "developer",
        }

    def test_super_admin_has_all_twenty_permissions(self):
        assert len(ROLE_PERMISSIONS["super_admin"]) == 20

    def test_only_super_admin_can_manage_roles(self):
        roles_with_manage_roles = {
            role for role, perms in ROLE_PERMISSIONS.items() if "manage_roles" in perms
        }
        assert roles_with_manage_roles == {"super_admin"}

    def test_only_super_admin_can_manage_security(self):
        roles_with_manage_security = {
            role for role, perms in ROLE_PERMISSIONS.items() if "manage_security" in perms
        }
        assert roles_with_manage_security == {"super_admin"}

    def test_admin_has_everything_except_roles_and_security(self):
        expected = ROLE_PERMISSIONS["super_admin"] - {"manage_roles", "manage_security"}
        assert ROLE_PERMISSIONS["admin"] == expected

    def test_editor_has_zero_user_data_access(self):
        user_data_permissions = {
            "view_users",
            "manage_users",
            "view_consents",
            "view_login_history",
            "manage_premium",
        }
        assert ROLE_PERMISSIONS["editor"].isdisjoint(user_data_permissions)

    def test_analyst_permissions_are_all_read_only(self):
        for permission in ROLE_PERMISSIONS["analyst"]:
            assert permission.startswith("view_")

    def test_every_role_has_at_least_one_permission(self):
        for role, perms in ROLE_PERMISSIONS.items():
            assert len(perms) > 0, f"{role} has no permissions"


class TestRoleHasPermission:
    def test_true_for_granted_permission(self):
        assert role_has_permission("editor", "view_content") is True

    def test_false_for_ungranted_permission(self):
        assert role_has_permission("editor", "view_users") is False

    def test_unknown_role_has_no_permissions(self):
        assert role_has_permission("nonexistent_role", "view_dashboard") is False  # type: ignore[arg-type]


class TestGetAdminRole:
    def test_returns_role_for_known_admin(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "supabase", _FakeSupabase(data=[{"role": "support"}]))
        assert get_admin_role("admin@example.com") == "support"

    def test_returns_none_when_no_row_exists(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "supabase", _FakeSupabase(data=[]))
        assert get_admin_role("nobody@example.com") is None

    def test_returns_none_on_db_failure_never_widens_access(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "supabase", _FakeSupabase(raise_error=True))
        assert get_admin_role("admin@example.com") is None

    def test_returns_none_for_unknown_role_value(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "supabase", _FakeSupabase(data=[{"role": "totally_made_up"}]))
        assert get_admin_role("admin@example.com") is None


class TestRequireAdminPermission:
    def test_raises_401_when_unauthenticated(self, monkeypatch):
        def _fake_require_email(authorization):
            raise HTTPException(status_code=401, detail="not authenticated")

        monkeypatch.setattr(rbac_module, "_require_email", _fake_require_email)
        with pytest.raises(HTTPException) as exc_info:
            require_admin_permission(None, "view_dashboard")
        assert exc_info.value.status_code == 401

    def test_raises_403_when_not_an_admin(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "_require_email", lambda auth: "user@example.com")
        monkeypatch.setattr(rbac_module, "get_admin_role", lambda email: None)
        with pytest.raises(HTTPException) as exc_info:
            require_admin_permission("Bearer x", "view_dashboard")
        assert exc_info.value.status_code == 403

    def test_raises_403_when_admin_lacks_specific_permission(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "_require_email", lambda auth: "editor@example.com")
        monkeypatch.setattr(rbac_module, "get_admin_role", lambda email: "editor")
        with pytest.raises(HTTPException) as exc_info:
            require_admin_permission("Bearer x", "view_users")
        assert exc_info.value.status_code == 403

    def test_returns_principal_on_success(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "_require_email", lambda auth: "super@example.com")
        monkeypatch.setattr(rbac_module, "get_admin_role", lambda email: "super_admin")
        principal = require_admin_permission("Bearer x", "manage_roles")
        assert principal == AdminPrincipal(email="super@example.com", role="super_admin")


class TestRequireAdmin:
    def test_raises_401_when_unauthenticated(self, monkeypatch):
        def _fake_require_email(authorization):
            raise HTTPException(status_code=401, detail="not authenticated")

        monkeypatch.setattr(rbac_module, "_require_email", _fake_require_email)
        with pytest.raises(HTTPException) as exc_info:
            require_admin(None)
        assert exc_info.value.status_code == 401

    def test_raises_403_when_not_an_admin(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "_require_email", lambda auth: "user@example.com")
        monkeypatch.setattr(rbac_module, "get_admin_role", lambda email: None)
        with pytest.raises(HTTPException) as exc_info:
            require_admin("Bearer x")
        assert exc_info.value.status_code == 403

    def test_returns_principal_for_any_admin_role(self, monkeypatch):
        monkeypatch.setattr(rbac_module, "_require_email", lambda auth: "editor@example.com")
        monkeypatch.setattr(rbac_module, "get_admin_role", lambda email: "editor")
        assert require_admin("Bearer x") == AdminPrincipal(email="editor@example.com", role="editor")
