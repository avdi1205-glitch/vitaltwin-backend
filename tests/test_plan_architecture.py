"""Unit tests for the VitalTwin Plan System (Free/Premium/Pro/Family):
`app.core.plan_service` (central entitlement logic) and the plan-aware
parts of `app.routers.users` (`set_premium_by_email` guard,
`activate_beta`). Mocks Supabase — no real network/database access.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import plan_service
from app.routers import users as users_module


class _FakeUsersQuery:
    def __init__(self, store: dict[str, dict[str, object]]):
        self._store = store
        self._filtered_email: str | None = None

    def select(self, *args, **kwargs):
        return self

    def update(self, payload: dict[str, object]):
        self._pending_update = payload
        return self

    def eq(self, field, value):
        if field == "email":
            self._filtered_email = value
            if hasattr(self, "_pending_update"):
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
def fake_users_supabase(monkeypatch):
    fake = _FakeUsersSupabase()
    monkeypatch.setattr(plan_service, "supabase", fake)
    return fake


@pytest.fixture
def fake_users_module_supabase(monkeypatch):
    """`set_premium_by_email`/`_get_user`/`_db_get_user` etc. all use
    `users.py`'s OWN `supabase` binding (a separate module-level import from
    `plan_service`'s) — this fixture patches that one instead, with the
    same fake query/store shape."""
    fake = _FakeUsersSupabase()
    monkeypatch.setattr(users_module, "supabase", fake)
    return fake


@pytest.fixture(autouse=True)
def _clear_users_cache():
    """`users.py` keeps an in-process `users_store` cache — clear it before
    and after every test so tests never see another test's cached user."""
    users_module.users_store.clear()
    yield
    users_module.users_store.clear()


class TestGetPlanByEmail:
    def test_returns_stored_plan(self, fake_users_supabase):
        fake_users_supabase.rows["pro@example.com"] = {"email": "pro@example.com", "plan": "pro", "premium": True}
        assert plan_service.get_plan_by_email("pro@example.com") == "pro"

    def test_defaults_to_free_for_unknown_user(self, fake_users_supabase):
        assert plan_service.get_plan_by_email("nobody@example.com") == "free"

    def test_falls_back_to_legacy_premium_boolean_when_plan_missing(self, fake_users_supabase):
        fake_users_supabase.rows["legacy@example.com"] = {"email": "legacy@example.com", "premium": True}
        assert plan_service.get_plan_by_email("legacy@example.com") == "premium"

    def test_normalizes_unexpected_plan_values_to_free(self, fake_users_supabase):
        fake_users_supabase.rows["weird@example.com"] = {"email": "weird@example.com", "plan": "enterprise", "premium": False}
        assert plan_service.get_plan_by_email("weird@example.com") == "free"

    def test_email_is_normalized_before_lookup(self, fake_users_supabase):
        fake_users_supabase.rows["case@example.com"] = {"email": "case@example.com", "plan": "premium"}
        assert plan_service.get_plan_by_email("  Case@Example.com  ") == "premium"


class _ColumnMissingQuery:
    """Simulates Postgrest rejecting a select that references a column
    which does not exist yet (migration 027 not yet run in Supabase)."""

    def __init__(self, row: dict[str, object] | None):
        self._row = row
        self._requested_columns = ""

    def select(self, columns: str):
        self._requested_columns = columns
        return self

    def eq(self, field, value):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        if "plan" in self._requested_columns.split(","):
            raise Exception('column "plan" does not exist')
        return SimpleNamespace(data=[self._row] if self._row else [])


class _ColumnMissingSupabase:
    def __init__(self, row: dict[str, object] | None):
        self._row = row

    def table(self, name):
        return _ColumnMissingQuery(self._row)


class TestGetPlanByEmailBeforeMigrationRuns:
    """Migration 027 (adds `vt_users.plan`) may not have been run in
    Supabase yet when this code is deployed (this repo's established
    pattern) — `get_plan_by_email` must keep working correctly off the
    legacy `premium` boolean alone until then, not break/crash."""

    def test_falls_back_to_premium_boolean_when_plan_column_does_not_exist_yet(self, monkeypatch):
        monkeypatch.setattr(plan_service, "supabase", _ColumnMissingSupabase({"premium": True}))
        assert plan_service.get_plan_by_email("premium@example.com") == "premium"

    def test_falls_back_to_free_when_plan_column_missing_and_not_premium(self, monkeypatch):
        monkeypatch.setattr(plan_service, "supabase", _ColumnMissingSupabase({"premium": False}))
        assert plan_service.get_plan_by_email("free@example.com") == "free"

    def test_falls_back_to_free_for_unknown_user_even_without_plan_column(self, monkeypatch):
        monkeypatch.setattr(plan_service, "supabase", _ColumnMissingSupabase(None))
        assert plan_service.get_plan_by_email("nobody@example.com") == "free"


class TestFeatureHierarchy:
    def test_free_does_not_have_extended_history(self):
        assert "extended_history" not in plan_service.FEATURE_SETS["free"]

    def test_premium_has_extended_history_and_cgm(self):
        assert "extended_history" in plan_service.FEATURE_SETS["premium"]
        assert "cgm_nutrition" in plan_service.FEATURE_SETS["premium"]

    @pytest.mark.parametrize("higher_plan", ["pro", "family"])
    def test_pro_and_family_are_never_missing_a_premium_feature(self, higher_plan):
        """Structural invariant: Pro must never get less than Premium, and
        Family must never be worse than Premium for general wellness
        features — checked against every currently-defined feature, not
        just one example, so this stays true as features are added."""
        missing = plan_service.FEATURE_SETS["premium"] - plan_service.FEATURE_SETS[higher_plan]
        assert missing == set()

    def test_has_feature_true_for_premium_false_for_free(self, fake_users_supabase):
        fake_users_supabase.rows["p@example.com"] = {"email": "p@example.com", "plan": "premium"}
        fake_users_supabase.rows["f@example.com"] = {"email": "f@example.com", "plan": "free"}
        assert plan_service.has_feature("p@example.com", "extended_history") is True
        assert plan_service.has_feature("f@example.com", "extended_history") is False

    def test_has_feature_true_for_pro_and_family(self, fake_users_supabase):
        fake_users_supabase.rows["pro@example.com"] = {"email": "pro@example.com", "plan": "pro"}
        fake_users_supabase.rows["fam@example.com"] = {"email": "fam@example.com", "plan": "family"}
        assert plan_service.has_feature("pro@example.com", "extended_history") is True
        assert plan_service.has_feature("fam@example.com", "extended_history") is True


class TestSetPlanByEmail:
    def test_rejects_unknown_plan(self):
        with pytest.raises(ValueError):
            plan_service.set_plan_by_email("x@example.com", "enterprise")  # type: ignore[arg-type]

    def test_writes_the_plan_column(self, fake_users_supabase, monkeypatch):
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: True)
        assert plan_service.set_plan_by_email("x@example.com", "pro") is True
        assert fake_users_supabase.rows["x@example.com"]["plan"] == "pro"

    def test_keeps_legacy_premium_boolean_in_sync_via_users_module(self, fake_users_supabase, monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: calls.append((email, premium)))
        plan_service.set_plan_by_email("x@example.com", "family")
        assert calls == [("x@example.com", True)]

    def test_setting_free_syncs_premium_false(self, fake_users_supabase, monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: calls.append((email, premium)))
        plan_service.set_plan_by_email("x@example.com", "free")
        assert calls == [("x@example.com", False)]

    def test_syncs_in_process_users_store_cache_so_me_endpoint_is_never_stale(self, fake_users_supabase, monkeypatch):
        """Regression test: `/api/users/me` reads `users.py`'s in-process
        `users_store` cache first (see `_get_user`). Admin Tarif-Wechsel and
        the Stripe webhook both go through this function, which used to
        only write the DB `plan` column — leaving a cached user's `plan`
        stuck at a stale value (observed live: an account already cached
        as "premium" stayed "premium" for `/me` even after being set to
        "pro" here, since only the DB row changed)."""
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: True)
        users_module.users_store["x@example.com"] = {"plan": "premium", "premium": True, "password": "h", "full_name": "X"}
        plan_service.set_plan_by_email("x@example.com", "pro")
        assert users_module.users_store["x@example.com"]["plan"] == "pro"
        assert users_module.users_store["x@example.com"]["premium"] is True

    def test_does_not_sync_cache_when_the_db_write_matches_zero_rows(self, monkeypatch):
        """Guards the `if updated:` check itself: a Postgrest UPDATE that
        matches nothing must not falsely mark the stale cache as correct."""

        class _ZeroRowQuery:
            def select(self, *args, **kwargs):
                return self

            def update(self, payload):
                return self

            def eq(self, field, value):
                return self

            def limit(self, *args, **kwargs):
                return self

            def execute(self):
                return SimpleNamespace(data=[])  # UPDATE matched no row — no exception, just empty data

        class _ZeroRowSupabase:
            def table(self, name):
                return _ZeroRowQuery()

        monkeypatch.setattr(plan_service, "supabase", _ZeroRowSupabase())
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: True)
        users_module.users_store["ghost@example.com"] = {"plan": "premium", "premium": True}
        assert plan_service.set_plan_by_email("ghost@example.com", "pro") is False
        assert users_module.users_store["ghost@example.com"]["plan"] == "premium"


class TestResolvePlanFromPriceId:
    def test_matches_configured_premium_price(self, monkeypatch):
        monkeypatch.setenv("STRIPE_PRICE_PREMIUM_MONTHLY", "price_premium_m")
        assert plan_service.resolve_plan_from_price_id("price_premium_m") == "premium"

    def test_matches_configured_pro_price(self, monkeypatch):
        monkeypatch.setenv("STRIPE_PRICE_PRO_YEARLY", "price_pro_y")
        assert plan_service.resolve_plan_from_price_id("price_pro_y") == "pro"

    def test_matches_configured_family_price(self, monkeypatch):
        monkeypatch.setenv("STRIPE_PRICE_FAMILY_MONTHLY", "price_family_m")
        assert plan_service.resolve_plan_from_price_id("price_family_m") == "family"

    def test_legacy_env_var_resolves_to_premium(self, monkeypatch):
        monkeypatch.delenv("STRIPE_PRICE_PREMIUM_MONTHLY", raising=False)
        monkeypatch.setenv("STRIPE_PRICE_ID", "price_legacy")
        assert plan_service.resolve_plan_from_price_id("price_legacy") == "premium"

    def test_unknown_price_id_returns_none(self, monkeypatch):
        monkeypatch.delenv("STRIPE_PRICE_ID", raising=False)
        assert plan_service.resolve_plan_from_price_id("price_totally_unknown") is None

    def test_none_price_id_returns_none(self):
        assert plan_service.resolve_plan_from_price_id(None) is None


class TestSetPremiumByEmailPlanAwareGuard:
    """`set_premium_by_email` (users.py) is called by the admin toggle,
    Stripe checkout-completed, and Beta-Zugang — it must never silently
    downgrade a real paying Pro/Family customer to plain Premium."""

    def test_upgrading_a_free_user_sets_plan_premium(self, fake_users_module_supabase):
        fake_users_module_supabase.rows["free@example.com"] = {
            "email": "free@example.com", "full_name": "x", "password": "y", "plan": "free", "premium": False, "suspended": False,
        }
        users_module.set_premium_by_email("free@example.com", True)
        assert fake_users_module_supabase.rows["free@example.com"]["plan"] == "premium"

    def test_upgrading_does_not_downgrade_an_existing_pro_plan(self, fake_users_module_supabase):
        fake_users_module_supabase.rows["pro@example.com"] = {
            "email": "pro@example.com", "full_name": "x", "password": "y", "plan": "pro", "premium": True, "suspended": False,
        }
        users_module.set_premium_by_email("pro@example.com", True)
        assert fake_users_module_supabase.rows["pro@example.com"]["plan"] == "pro"

    def test_upgrading_does_not_downgrade_an_existing_family_plan(self, fake_users_module_supabase):
        fake_users_module_supabase.rows["fam@example.com"] = {
            "email": "fam@example.com", "full_name": "x", "password": "y", "plan": "family", "premium": True, "suspended": False,
        }
        users_module.set_premium_by_email("fam@example.com", True)
        assert fake_users_module_supabase.rows["fam@example.com"]["plan"] == "family"

    def test_downgrading_always_resets_plan_to_free(self, fake_users_module_supabase):
        fake_users_module_supabase.rows["pro@example.com"] = {
            "email": "pro@example.com", "full_name": "x", "password": "y", "plan": "pro", "premium": True, "suspended": False,
        }
        users_module.set_premium_by_email("pro@example.com", False)
        assert fake_users_module_supabase.rows["pro@example.com"]["plan"] == "free"


class TestDbGetUserBeforeMigrationRuns:
    """`_db_get_user` must keep returning a usable row (with the legacy
    `premium` boolean) even if migration 027 has not been run in Supabase
    yet — selecting a non-existent `plan` column must not make every
    existing user look logged-out/non-premium."""

    def test_falls_back_to_pre_migration_columns(self, monkeypatch):
        monkeypatch.setattr(
            users_module,
            "supabase",
            _ColumnMissingSupabase({"email": "user@example.com", "full_name": "Real User", "password": "hash", "premium": True, "suspended": False}),
        )
        row = users_module._db_get_user("user@example.com")
        assert row is not None
        assert row["premium"] is True


class TestActivateBeta:
    @pytest.mark.anyio
    async def test_activates_beta_for_a_free_user(self, monkeypatch):
        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: "free@example.com")
        monkeypatch.setattr(users_module, "_get_user", lambda email: {"premium": False, "plan": "free"})
        calls: list[tuple] = []
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: (calls.append((email, premium)), True)[1])

        result = await users_module.activate_beta(authorization="Bearer faketoken")
        assert result["premium"] is True
        assert result["plan"] == "premium"
        assert calls == [("free@example.com", True)]

    @pytest.mark.anyio
    async def test_does_not_downgrade_an_existing_pro_user(self, monkeypatch):
        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: "pro@example.com")
        monkeypatch.setattr(users_module, "_get_user", lambda email: {"premium": True, "plan": "pro"})
        calls: list[tuple] = []
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: calls.append((email, premium)))

        result = await users_module.activate_beta(authorization="Bearer faketoken")
        assert result["plan"] == "pro"
        assert calls == []  # never touched a real paying Pro account

    @pytest.mark.anyio
    async def test_does_not_downgrade_an_existing_family_user(self, monkeypatch):
        monkeypatch.setattr(users_module, "get_email_by_token", lambda token: "fam@example.com")
        monkeypatch.setattr(users_module, "_get_user", lambda email: {"premium": True, "plan": "family"})
        calls: list[tuple] = []
        monkeypatch.setattr(users_module, "set_premium_by_email", lambda email, premium: calls.append((email, premium)))

        result = await users_module.activate_beta(authorization="Bearer faketoken")
        assert result["plan"] == "family"
        assert calls == []

    @pytest.mark.anyio
    async def test_requires_auth_header(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await users_module.activate_beta(authorization=None)
        assert exc_info.value.status_code == 401
