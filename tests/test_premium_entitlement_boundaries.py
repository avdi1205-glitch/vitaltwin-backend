"""Unit tests for the Premium Quality Completion round: real server-side
entitlement boundaries added for two customer-facing endpoints —
`profile.py::get_personal_baseline` ("Ausführlichere Wellness-
Auswertungen") and `daily_planning.py::get_weekly_reflection`
("Wochenberichte"). Both use the existing VitalTwin Plan System
(`core/plan_service.py::has_feature`) — no parallel entitlement system.

Mocks Supabase and auth/entitlement lookup — no real network/database
access. Free must be denied (403); Premium/Pro/Family must be allowed
(inheritance via the existing FEATURE_SETS superset architecture, not
duplicated per-tier logic)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import profile as profile_module
from app.routers import daily_planning as daily_planning_module


class _AnyTableQuery:
    """Accepts any chain of .select()/.eq()/.order()/.limit()/.is_() calls
    and always returns empty data — sufficient for testing an entitlement
    gate without needing per-table computation accuracy (already covered
    by dedicated unit tests for the underlying service functions)."""

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def update(self, *args, **kwargs):
        return self

    def insert(self, *args, **kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _AnyTableSupabase:
    def table(self, name):
        return _AnyTableQuery()


class TestDetailedWellnessEntitlement:
    @pytest.mark.anyio
    async def test_free_user_denied(self, monkeypatch):
        monkeypatch.setattr(profile_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free-user@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.get_personal_baseline(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_premium_user_allowed(self, monkeypatch):
        monkeypatch.setattr(profile_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "premium-user@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.get_personal_baseline(authorization="Bearer x")
        assert "items" in result

    @pytest.mark.anyio
    async def test_pro_and_family_allowed(self, monkeypatch):
        monkeypatch.setattr(profile_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        for email in ("pro-user@example.com", "family-user@example.com"):
            monkeypatch.setattr(profile_module, "_require_email", lambda auth, e=email: e)
            result = await profile_module.get_personal_baseline(authorization="Bearer x")
            assert "items" in result

    @pytest.mark.anyio
    async def test_gating_uses_the_callers_own_authenticated_email(self, monkeypatch):
        """No frontend-only lock: `has_feature` is always looked up
        server-side from the caller's own email, never a client-supplied
        value."""
        monkeypatch.setattr(profile_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free-user@example.com")

        seen_emails: list[str] = []

        def fake_has_feature(email: str, feature: str) -> bool:
            seen_emails.append(email)
            return False

        monkeypatch.setattr(profile_module, "has_feature", fake_has_feature)

        with pytest.raises(HTTPException):
            await profile_module.get_personal_baseline(authorization="Bearer x")
        assert seen_emails == ["free-user@example.com"]


class TestWeeklyReportsEntitlement:
    @pytest.mark.anyio
    async def test_free_user_denied(self, monkeypatch):
        monkeypatch.setattr(daily_planning_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(daily_planning_module, "_require_email", lambda auth: "free-user@example.com")
        monkeypatch.setattr(daily_planning_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await daily_planning_module.get_weekly_reflection(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_premium_user_allowed(self, monkeypatch):
        monkeypatch.setattr(daily_planning_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(daily_planning_module, "_require_email", lambda auth: "premium-user@example.com")
        monkeypatch.setattr(daily_planning_module, "has_feature", lambda email, feature: True)

        result = await daily_planning_module.get_weekly_reflection(authorization="Bearer x")
        assert result["data_sufficient"] is False
        assert result["email"] == "premium-user@example.com"

    @pytest.mark.anyio
    async def test_pro_and_family_allowed(self, monkeypatch):
        monkeypatch.setattr(daily_planning_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(daily_planning_module, "has_feature", lambda email, feature: True)

        for email in ("pro-user@example.com", "family-user@example.com"):
            monkeypatch.setattr(daily_planning_module, "_require_email", lambda auth, e=email: e)
            result = await daily_planning_module.get_weekly_reflection(authorization="Bearer x")
            assert result["email"] == email

    @pytest.mark.anyio
    async def test_gating_uses_the_callers_own_authenticated_email(self, monkeypatch):
        monkeypatch.setattr(daily_planning_module, "supabase", _AnyTableSupabase())
        monkeypatch.setattr(daily_planning_module, "_require_email", lambda auth: "free-user@example.com")

        seen_emails: list[str] = []

        def fake_has_feature(email: str, feature: str) -> bool:
            seen_emails.append(email)
            return False

        monkeypatch.setattr(daily_planning_module, "has_feature", fake_has_feature)

        with pytest.raises(HTTPException):
            await daily_planning_module.get_weekly_reflection(authorization="Bearer x")
        assert seen_emails == ["free-user@example.com"]


class TestSharedPrimitivesRemainUngated:
    """Regression coverage: the new gates must NOT touch the shared
    service functions other already-gated Pro/Family features depend on
    directly (bypassing these two router endpoints entirely)."""

    def test_build_personal_baseline_report_has_no_entitlement_check(self):
        from app.services.personal_baseline import build_personal_baseline_report
        from datetime import date

        # Callable directly with zero auth/plan context — proves it stays
        # a pure, reusable service function (used directly by
        # advanced_twin_overview.py/thirty_day_report.py).
        report = build_personal_baseline_report([], date(2026, 1, 1))
        assert "items" in report

    def test_compute_weekly_reflection_has_no_entitlement_check(self):
        from app.services.weekly_reflection import compute_weekly_reflection

        result = compute_weekly_reflection(
            this_week_entries=[],
            previous_week_entries=[],
            habits=[],
            goals=[],
            recommendation_history=[],
            confirmed_patterns=[],
        )
        assert result.data_sufficient is False
