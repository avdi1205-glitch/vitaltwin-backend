"""Unit tests for the Pro feature "Mehrere persönliche Ziele" (multiple
simultaneously active wellness goals) — `app.routers.profile::create_goal`
and `::update_goal`. Free/Premium stay capped at
`plan_service.FREE_TIER_MAX_ACTIVE_GOALS` (matches Premium's own pricing
bullet "Individuelle Tagesziele", singular); Pro/Family get the
`multiple_goals` feature (unlimited). Mocks Supabase — no real network.

Enforcement never touches a goal a user already has — it only blocks NEW
activations (create with status="active", or PATCH to status="active")."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import profile as profile_module
from app.routers.profile import GoalCreate, GoalUpdate
from app.core.plan_service import FREE_TIER_MAX_ACTIVE_GOALS, get_active_goal_limit


class _FakeGoalQuery:
    def __init__(self, calls_log, existing_goal=None, active_count=0):
        self._calls_log = calls_log
        self._existing_goal = existing_goal
        self._active_count = active_count
        self._filters: dict[str, object] = {}
        self._op: str | None = None
        self._payload: dict[str, object] | None = None

    def select(self, *args, **kwargs):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    def is_(self, field, value):
        self._filters[field] = value
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, value):
        self._filters["limit"] = value
        return self

    def execute(self):
        self._calls_log.append({"op": self._op, "filters": dict(self._filters)})
        if self._op == "insert":
            return SimpleNamespace(data=[{**self._payload, "id": "new-goal-id"}])
        if self._op == "update":
            # Mutates the SAME dict `_FakeGoalSupabase` hands to every query,
            # so a later `_require_own_goal` re-select (real update_goal's
            # return path) sees the change — not a stale pre-update copy.
            if self._existing_goal is not None:
                self._existing_goal.update(self._payload)
            return SimpleNamespace(data=[dict(self._existing_goal or self._payload)])
        # select
        if self._filters.get("status") == "active":
            return SimpleNamespace(data=[{"id": f"g{i}"} for i in range(self._active_count)])
        if "id" in self._filters:
            return SimpleNamespace(data=[self._existing_goal] if self._existing_goal else [])
        return SimpleNamespace(data=[])


class _FakeGoalSupabase:
    def __init__(self, existing_goal=None, active_count=0):
        self.calls: list[dict[str, object]] = []
        self._existing_goal = existing_goal
        self._active_count = active_count

    def table(self, name):
        return _FakeGoalQuery(self.calls, self._existing_goal, self._active_count)


class TestGetActiveGoalLimit:
    def test_free_and_premium_are_capped(self, monkeypatch):
        from app.core import plan_service

        monkeypatch.setattr(plan_service, "get_plan_by_email", lambda email: "free")
        assert get_active_goal_limit("user@example.com") == FREE_TIER_MAX_ACTIVE_GOALS

        monkeypatch.setattr(plan_service, "get_plan_by_email", lambda email: "premium")
        assert get_active_goal_limit("user@example.com") == FREE_TIER_MAX_ACTIVE_GOALS

    def test_pro_and_family_are_unlimited(self, monkeypatch):
        from app.core import plan_service

        monkeypatch.setattr(plan_service, "get_plan_by_email", lambda email: "pro")
        assert get_active_goal_limit("user@example.com") is None

        monkeypatch.setattr(plan_service, "get_plan_by_email", lambda email: "family")
        assert get_active_goal_limit("user@example.com") is None


class TestCreateGoalActiveLimit:
    @pytest.mark.anyio
    async def test_free_user_can_create_first_active_goal(self, monkeypatch):
        fake = _FakeGoalSupabase(active_count=0)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "get_active_goal_limit", lambda email: 1)

        result = await profile_module.create_goal(
            GoalCreate(title="Mehr schlafen", goal_type="besser_schlafen"), authorization="Bearer x"
        )
        assert result["title"] == "Mehr schlafen"

    @pytest.mark.anyio
    async def test_free_user_is_blocked_from_a_second_active_goal(self, monkeypatch):
        fake = _FakeGoalSupabase(active_count=1)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "get_active_goal_limit", lambda email: 1)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.create_goal(
                GoalCreate(title="Mehr bewegen", goal_type="mehr_bewegen"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_free_user_can_still_create_a_paused_goal_while_at_the_limit(self, monkeypatch):
        """Only ACTIVE goals count against the cap — creating a goal with a
        non-active status must never be blocked."""
        fake = _FakeGoalSupabase(active_count=1)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "get_active_goal_limit", lambda email: 1)

        result = await profile_module.create_goal(
            GoalCreate(title="Später", goal_type="mehr_energie", status="paused"), authorization="Bearer x"
        )
        assert result["status"] == "paused"

    @pytest.mark.anyio
    async def test_pro_user_can_create_many_active_goals(self, monkeypatch):
        fake = _FakeGoalSupabase(active_count=5)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "pro@example.com")
        monkeypatch.setattr(profile_module, "get_active_goal_limit", lambda email: None)

        result = await profile_module.create_goal(
            GoalCreate(title="Ziel Nummer 6", goal_type="eigenes_ziel"), authorization="Bearer x"
        )
        assert result["title"] == "Ziel Nummer 6"

    @pytest.mark.anyio
    async def test_limit_check_is_scoped_to_the_requesting_users_own_email(self, monkeypatch):
        fake = _FakeGoalSupabase(active_count=0)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")

        seen_emails: list[str] = []

        def fake_limit(email: str) -> int | None:
            seen_emails.append(email)
            return 1

        monkeypatch.setattr(profile_module, "get_active_goal_limit", fake_limit)

        await profile_module.create_goal(GoalCreate(title="Ziel", goal_type="eigenes_ziel"), authorization="Bearer x")
        assert seen_emails == ["user-a@example.com"]


class TestUpdateGoalActiveLimit:
    @pytest.mark.anyio
    async def test_free_user_cannot_activate_a_second_goal_while_one_is_already_active(self, monkeypatch):
        existing = {"id": "g2", "email": "free@example.com", "title": "Zweites Ziel", "status": "paused"}
        fake = _FakeGoalSupabase(existing_goal=existing, active_count=1)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "get_active_goal_limit", lambda email: 1)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.update_goal("g2", GoalUpdate(status="active"), authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_free_user_can_reactivate_their_only_goal(self, monkeypatch):
        """Re-activating the SAME goal that just became active (no other
        active goal exists) must not be blocked."""
        existing = {"id": "g1", "email": "free@example.com", "title": "Erstes Ziel", "status": "paused"}
        fake = _FakeGoalSupabase(existing_goal=existing, active_count=0)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "get_active_goal_limit", lambda email: 1)

        result = await profile_module.update_goal("g1", GoalUpdate(status="active"), authorization="Bearer x")
        assert result["status"] == "active"

    @pytest.mark.anyio
    async def test_updating_a_goal_that_is_already_active_is_never_blocked(self, monkeypatch):
        """Changing e.g. only the title of an ALREADY-active goal must not
        re-trigger the limit check (no status transition is happening)."""
        existing = {"id": "g1", "email": "free@example.com", "title": "Altes Ziel", "status": "active"}
        fake = _FakeGoalSupabase(existing_goal=existing, active_count=1)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")

        def fail_if_called(email: str) -> int | None:
            raise AssertionError("get_active_goal_limit must not be called for a non-activation update")

        monkeypatch.setattr(profile_module, "get_active_goal_limit", fail_if_called)

        result = await profile_module.update_goal("g1", GoalUpdate(title="Neuer Titel"), authorization="Bearer x")
        assert result["title"] == "Neuer Titel"

    @pytest.mark.anyio
    async def test_pro_user_can_activate_freely(self, monkeypatch):
        existing = {"id": "g9", "email": "pro@example.com", "title": "Ziel 9", "status": "paused"}
        fake = _FakeGoalSupabase(existing_goal=existing, active_count=5)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "pro@example.com")
        monkeypatch.setattr(profile_module, "get_active_goal_limit", lambda email: None)

        result = await profile_module.update_goal("g9", GoalUpdate(status="active"), authorization="Bearer x")
        assert result["status"] == "active"
