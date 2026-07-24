"""Tests for the Founder Dashboard (VitalTwin Release F1 — Founder
Operating System, Module 1): `routers/founder.py::founder_dashboard` —
a single read-only aggregation endpoint. No automation/AI/background work
lives here, so these tests only check (1) the permission requirement and
(2) that every field is either a real computed number or an honest
`None` + note — never a fabricated placeholder."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routers import founder as founder_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeFounderQuery:
    def __init__(self, data, count=None):
        self._data = data
        self._count = count
        self._raise = False

    def select(self, *a, count=None, **k):
        return self

    def eq(self, field, value):
        filtered = [row for row in self._data if row.get(field) == value]
        return _FakeFounderQuery(filtered, count=len(filtered))

    def gte(self, *a, **k):
        return self

    def execute(self):
        if self._raise:
            raise RuntimeError("boom")
        count = self._count if self._count is not None else len(self._data)
        return SimpleNamespace(data=self._data, count=count)


class _FakeFounderSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.get(name, [])
        return _FakeFounderQuery(rows, count=len(rows))


@pytest.fixture
def founder_supabase(monkeypatch):
    fake = _FakeFounderSupabase(
        {
            "vt_users": [
                {"email": "a@example.com", "premium": True, "created_at": "2099-01-01"},
                {"email": "b@example.com", "premium": False, "created_at": "2000-01-01"},
            ],
            "vt_daily_wellness_entries": [{"email": "a@example.com", "entry_date": "2099-01-01"}],
            "vt_chat_usage": [{"count": 3}, {"count": 5}],
            "vt_affiliate_products": [
                {"status": "active", "link_status": "ok"},
                {"status": "in_review", "link_status": "ok"},
                {"status": "active", "link_status": "broken"},
            ],
            "vt_affiliate_events": [
                {"event_type": "conversion", "revenue": 19.99},
                {"event_type": "conversion", "revenue": 5.0},
                {"event_type": "click", "revenue": None},
            ],
        }
    )
    monkeypatch.setattr(founder_module, "supabase", fake)
    return fake


@pytest.fixture
def founder_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(founder_module, "require_admin_permission", _fake)
    return calls


class TestFounderDashboard:
    @pytest.mark.anyio
    async def test_requires_view_founder_dashboard_permission(self, founder_supabase, founder_permission_spy):
        await founder_module.founder_dashboard(authorization="Bearer x")
        assert founder_permission_spy[-1] == ("Bearer x", "view_founder_dashboard")

    @pytest.mark.anyio
    async def test_users_card_has_real_counts(self, founder_supabase, founder_permission_spy):
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["users"]["total"] == 2
        assert result["users"]["premium"] == 1
        assert result["users"]["active_7d"] == 1

    @pytest.mark.anyio
    async def test_affiliate_revenue_is_real_sum(self, founder_supabase, founder_permission_spy):
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["revenue"]["affiliate"] == pytest.approx(24.99)

    @pytest.mark.anyio
    async def test_stripe_and_premium_revenue_are_honestly_none(self, founder_supabase, founder_permission_spy):
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["revenue"]["stripe"] is None
        assert result["revenue"]["premium"] is None
        assert "note" in result["revenue"]["stripe_note"] or result["revenue"]["stripe_note"]
        assert result["revenue"]["premium_note"]

    @pytest.mark.anyio
    async def test_ai_card_has_no_fabricated_cost_or_errors(self, founder_supabase, founder_permission_spy):
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["ai"]["requests_total"] == 8
        assert result["ai"]["errors"] is None
        assert result["ai"]["cost"] is None

    @pytest.mark.anyio
    async def test_affiliate_card_counts_by_status(self, founder_supabase, founder_permission_spy):
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["affiliate"]["active_products"] == 2
        assert result["affiliate"]["pending_approval"] == 1
        assert result["affiliate"]["broken_links"] == 1

    @pytest.mark.anyio
    async def test_system_card_reports_reachable_database(self, founder_supabase, founder_permission_spy):
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["system"]["database"] == "reachable"
        assert result["system"]["api"] == "online"
        assert result["system"]["server"] is None
        assert result["system"]["build_status"] is None

    @pytest.mark.anyio
    async def test_tasks_card_reuses_real_counts_and_is_honest_about_missing_data(self, founder_supabase, founder_permission_spy):
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["tasks"]["products_to_review"] == 1
        assert result["tasks"]["broken_links"] == 1
        assert result["tasks"]["open_releases"] is None
        assert result["tasks"]["open_bugs"] is None

    @pytest.mark.anyio
    async def test_database_unreachable_is_reported_honestly(self, founder_permission_spy, monkeypatch):
        class _RaisingQuery:
            def select(self, *a, count=None, **k):
                raise RuntimeError("db down")

        class _RaisingSupabase:
            def table(self, name):
                return _RaisingQuery()

        monkeypatch.setattr(founder_module, "supabase", _RaisingSupabase())
        result = await founder_module.founder_dashboard(authorization="Bearer x")
        assert result["system"]["database"] == "unreachable"
        assert result["users"]["total"] is None
