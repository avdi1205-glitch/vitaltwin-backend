"""Unit tests for the Beta Tester Program (`app.core.plan_service`'s
`beta_*` overlay: grant/extend/revoke + effective-plan resolution).
Mocks Supabase — no real network/database access. Complements
`test_plan_architecture.py` (real-plan resolution, unchanged/untouched by
this feature).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core import plan_service


class _FakeUsersQuery:
    def __init__(self, store: dict[str, dict[str, object]]):
        self._store = store
        self._filtered_email: str | None = None
        self._pending_update: dict[str, object] | None = None

    def select(self, *args, **kwargs):
        return self

    def update(self, payload: dict[str, object]):
        self._pending_update = payload
        return self

    def eq(self, field, value):
        if field == "email":
            self._filtered_email = value
            if self._pending_update is not None:
                row = self._store.setdefault(value, {"email": value})
                row.update(self._pending_update)
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        if self._filtered_email is not None:
            row = self._store.get(self._filtered_email)
            return SimpleNamespace(data=[row] if row else [])
        return SimpleNamespace(data=list(self._store.values()))


class _FakeUsersSupabase:
    def __init__(self, rows: dict[str, dict[str, object]] | None = None):
        self.rows: dict[str, dict[str, object]] = rows or {}

    def table(self, name):
        assert name == "vt_users"
        return _FakeUsersQuery(self.rows)


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeUsersSupabase()
    monkeypatch.setattr(plan_service, "supabase", fake)
    return fake


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestGrantBetaByEmail:
    def test_rejects_invalid_plan(self, fake_supabase):
        with pytest.raises(ValueError):
            plan_service.grant_beta_by_email("x@example.com", "free", 30, granted_by="admin@example.com")

    def test_rejects_non_positive_days(self, fake_supabase):
        with pytest.raises(ValueError):
            plan_service.grant_beta_by_email("x@example.com", "pro", 0, granted_by="admin@example.com")

    def test_grants_pro_beta_and_sets_expiry(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "plan": "free"}
        assert plan_service.grant_beta_by_email("x@example.com", "pro", 90, granted_by="admin@example.com") is True
        row = fake_supabase.rows["x@example.com"]
        assert row["beta_plan"] == "pro"
        assert row["beta_granted_by"] == "admin@example.com"
        started = datetime.fromisoformat(row["beta_started_at"])
        expires = datetime.fromisoformat(row["beta_expires_at"])
        assert 89 <= (expires - started).days <= 90

    def test_never_touches_the_real_plan_column(self, fake_supabase):
        """Beta grant is an entitlement overlay only — the underlying paid
        `plan` column must never be written by this function."""
        fake_supabase.rows["paid@example.com"] = {"email": "paid@example.com", "plan": "premium", "premium": True}
        plan_service.grant_beta_by_email("paid@example.com", "pro", 30, granted_by="admin@example.com")
        assert fake_supabase.rows["paid@example.com"]["plan"] == "premium"

    def test_new_grant_replaces_a_previous_one(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "free", "beta_plan": "premium", "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=5)),
        }
        plan_service.grant_beta_by_email("x@example.com", "family", 60, granted_by="admin@example.com")
        assert fake_supabase.rows["x@example.com"]["beta_plan"] == "family"


class TestGetBetaGrantByEmail:
    def test_returns_none_when_never_granted(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "plan": "free"}
        assert plan_service.get_beta_grant_by_email("x@example.com") is None

    def test_returns_active_grant_with_remaining_days(self, fake_supabase):
        expires = datetime.now(timezone.utc) + timedelta(days=10)
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "beta_plan": "pro", "beta_started_at": _iso(datetime.now(timezone.utc)),
            "beta_expires_at": _iso(expires), "beta_granted_by": "admin@example.com",
        }
        grant = plan_service.get_beta_grant_by_email("x@example.com")
        assert grant is not None
        assert grant["plan"] == "pro"
        assert grant["active"] is True
        assert 9 <= grant["remaining_days"] <= 10

    def test_returns_inactive_for_an_expired_grant(self, fake_supabase):
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "beta_plan": "family", "beta_expires_at": _iso(expired),
        }
        grant = plan_service.get_beta_grant_by_email("x@example.com")
        assert grant is not None
        assert grant["active"] is False
        assert grant["remaining_days"] == 0

    def test_missing_beta_columns_before_migration_returns_none(self, monkeypatch):
        """Migration 034 may not have been run in Supabase yet when this
        code is deployed — the beta columns must degrade to 'no grant',
        never raise."""

        class _ColumnMissingQuery:
            def select(self, columns):
                self._columns = columns
                return self

            def eq(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def execute(self):
                raise Exception('column "beta_plan" does not exist')

        class _ColumnMissingSupabase:
            def table(self, name):
                return _ColumnMissingQuery()

        monkeypatch.setattr(plan_service, "supabase", _ColumnMissingSupabase())
        assert plan_service.get_beta_grant_by_email("x@example.com") is None


class TestExtendBetaByEmail:
    def test_returns_none_when_nothing_to_extend(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "plan": "free"}
        assert plan_service.extend_beta_by_email("x@example.com", 30, granted_by="admin@example.com") is None

    def test_extends_from_current_expiry_when_still_active(self, fake_supabase):
        expires = datetime.now(timezone.utc) + timedelta(days=5)
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "beta_plan": "pro", "beta_expires_at": _iso(expires)}
        updated = plan_service.extend_beta_by_email("x@example.com", 30, granted_by="admin@example.com")
        assert updated is not None
        new_expiry = datetime.fromisoformat(updated["expires_at"])
        assert 33 <= (new_expiry - datetime.now(timezone.utc)).days <= 35

    def test_extends_from_now_when_already_expired_never_backdates(self, fake_supabase):
        expired = datetime.now(timezone.utc) - timedelta(days=20)
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "beta_plan": "premium", "beta_expires_at": _iso(expired)}
        updated = plan_service.extend_beta_by_email("x@example.com", 30, granted_by="admin@example.com")
        new_expiry = datetime.fromisoformat(updated["expires_at"])
        assert 28 <= (new_expiry - datetime.now(timezone.utc)).days <= 30

    def test_rejects_non_positive_days(self, fake_supabase):
        with pytest.raises(ValueError):
            plan_service.extend_beta_by_email("x@example.com", 0, granted_by="admin@example.com")


class TestRevokeBetaByEmail:
    def test_returns_none_when_nothing_active(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "plan": "free"}
        assert plan_service.revoke_beta_by_email("x@example.com") is None

    def test_clears_all_beta_columns(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "free", "beta_plan": "pro",
            "beta_started_at": "2026-01-01T00:00:00+00:00",
            "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=10)),
            "beta_granted_by": "admin@example.com",
        }
        previous = plan_service.revoke_beta_by_email("x@example.com")
        assert previous is not None
        assert previous["plan"] == "pro"
        row = fake_supabase.rows["x@example.com"]
        assert row["beta_plan"] is None
        assert row["beta_started_at"] is None
        assert row["beta_expires_at"] is None
        assert row["beta_granted_by"] is None

    def test_never_touches_real_plan_or_deletes_data(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "premium", "beta_plan": "family",
            "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=10)),
        }
        plan_service.revoke_beta_by_email("x@example.com")
        assert fake_supabase.rows["x@example.com"]["plan"] == "premium"
        assert "email" in fake_supabase.rows["x@example.com"]


class TestGetEffectivePlanByEmail:
    def test_free_user_with_no_grant_stays_free(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "plan": "free"}
        assert plan_service.get_effective_plan_by_email("x@example.com") == "free"

    def test_active_pro_beta_grant_elevates_free_user(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "free", "beta_plan": "pro",
            "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=30)),
        }
        assert plan_service.get_effective_plan_by_email("x@example.com") == "pro"

    def test_active_family_beta_grant_elevates_premium_user(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "premium", "beta_plan": "family",
            "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=30)),
        }
        assert plan_service.get_effective_plan_by_email("x@example.com") == "family"

    def test_expired_grant_falls_back_to_real_plan(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "free", "beta_plan": "pro",
            "beta_expires_at": _iso(datetime.now(timezone.utc) - timedelta(days=1)),
        }
        assert plan_service.get_effective_plan_by_email("x@example.com") == "free"

    def test_beta_grant_never_downgrades_a_higher_real_plan(self, fake_supabase):
        """A real paying Pro customer accidentally granted 'Premium Beta'
        must keep Pro — a Beta grant only ever adds access, never removes."""
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "pro", "beta_plan": "premium",
            "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=30)),
        }
        assert plan_service.get_effective_plan_by_email("x@example.com") == "pro"

    def test_no_active_grant_uses_real_plan_only(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "plan": "family"}
        assert plan_service.get_effective_plan_by_email("x@example.com") == "family"


class TestHasFeatureIsBetaAware:
    """`has_feature()` is the single gate used by family.py/health.py/
    google_health.py/profile.py/daily_planning.py — this is the ONE
    integration point that makes a Beta grant unlock REAL entitlements
    everywhere, without touching any of those routers."""

    def test_pro_beta_grant_unlocks_a_real_pro_only_feature(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "free", "beta_plan": "pro",
            "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=30)),
        }
        assert plan_service.has_feature("x@example.com", "lifestyle_simulation") is True
        assert plan_service.has_feature("x@example.com", "advanced_digital_twin") is True

    def test_family_beta_grant_unlocks_family_profiles(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "free", "beta_plan": "family",
            "beta_expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=30)),
        }
        assert plan_service.has_feature("x@example.com", "family_profiles") is True
        assert plan_service.has_feature("x@example.com", "family_goals") is True
        assert plan_service.has_feature("x@example.com", "family_challenges") is True

    def test_expired_pro_beta_grant_no_longer_unlocks_pro_feature(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {
            "email": "x@example.com", "plan": "free", "beta_plan": "pro",
            "beta_expires_at": _iso(datetime.now(timezone.utc) - timedelta(days=1)),
        }
        assert plan_service.has_feature("x@example.com", "lifestyle_simulation") is False

    def test_free_user_without_any_grant_never_gets_pro_features(self, fake_supabase):
        fake_supabase.rows["x@example.com"] = {"email": "x@example.com", "plan": "free"}
        assert plan_service.has_feature("x@example.com", "advanced_digital_twin") is False
        assert plan_service.has_feature("x@example.com", "family_profiles") is False


class TestActivateBetaNeverGrantsProOrFamily:
    """The pre-existing self-service `/api/users/activate-beta` endpoint
    (free Beta-Zugang) must remain completely separate from the new
    admin-only Beta Tester Program — a normal user must never be able to
    self-grant Pro/Family via that or any other customer-facing path."""

    @pytest.mark.anyio
    async def test_self_service_activate_beta_only_ever_sets_premium(self, monkeypatch):
        from app.routers import users as users_module

        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: "free@example.com")
        monkeypatch.setattr(users_module, "_get_user", lambda email: {"premium": False, "plan": "free"})
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: True)

        result = await users_module.activate_beta(authorization="Bearer faketoken")
        assert result["plan"] == "premium"  # never "pro"/"family" from this endpoint


class TestAdminOnlyEndpointsRejectUnauthenticatedRequests:
    """Security: the real (non-mocked) `require_admin_permission` chain
    must reject grant/extend/revoke without valid admin authorization —
    proves a forged/missing token cannot grant Beta access, and that these
    endpoints are not reachable by an ordinary logged-in user."""

    @pytest.mark.anyio
    async def test_grant_requires_authorization_header(self):
        from fastapi import HTTPException
        from app.routers import admin as admin_module
        from app.routers.admin import BetaGrantInput

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.grant_beta_access("victim@example.com", BetaGrantInput(plan="family", days=90), authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_extend_requires_authorization_header(self):
        from fastapi import HTTPException
        from app.routers import admin as admin_module
        from app.routers.admin import BetaExtendInput

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.extend_beta_access("victim@example.com", BetaExtendInput(days=30), authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_revoke_requires_authorization_header(self):
        from fastapi import HTTPException
        from app.routers import admin as admin_module

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.revoke_beta_access("victim@example.com", authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_forged_bearer_token_for_unknown_admin_is_rejected(self):
        """A syntactically valid but non-admin JWT must not grant access —
        `require_admin_permission` looks the email up in `vt_admin_roles`
        and 403s when absent, regardless of what the token itself claims."""
        from fastapi import HTTPException
        from app.routers import admin as admin_module
        from app.routers.admin import BetaGrantInput

        with pytest.raises(HTTPException) as exc_info:
            await admin_module.grant_beta_access(
                "victim@example.com", BetaGrantInput(plan="family", days=90), authorization="Bearer not-a-real-admin-token"
            )
        assert exc_info.value.status_code in (401, 403)


@pytest.fixture
def anyio_backend():
    return "asyncio"
