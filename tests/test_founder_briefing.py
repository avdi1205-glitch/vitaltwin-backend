"""Tests for the Founder Daily Briefing (VitalTwin Release F2 — Founder
Operating System, Module 2): `routers/founder_briefing.py`. Focuses on
(1) the permission requirement, (2) that every honest "no data" field
stays `None` with a note instead of a fabricated number, and (3) that the
rule-based warnings/recommendations/priorities only fire when a real
threshold is actually crossed (no spam)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routers import founder_briefing as briefing_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeBriefingQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, count=None, **k):
        return self

    def eq(self, field, value):
        return _FakeBriefingQuery([row for row in self._data if row.get(field) == value])

    def gte(self, field, value):
        return _FakeBriefingQuery([row for row in self._data if str(row.get(field, "")) >= value])

    def execute(self):
        return SimpleNamespace(data=self._data, count=len(self._data))


class _FakeBriefingSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        return _FakeBriefingQuery(self.tables.get(name, []))


@pytest.fixture
def briefing_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(briefing_module, "require_admin_permission", _fake)
    return calls


def _empty_supabase(monkeypatch):
    fake = _FakeBriefingSupabase()
    monkeypatch.setattr(briefing_module, "supabase", fake)
    return fake


class TestFounderDailyBriefing:
    @pytest.mark.anyio
    async def test_requires_view_founder_briefing_permission(self, briefing_permission_spy, monkeypatch):
        _empty_supabase(monkeypatch)
        await briefing_module.founder_daily_briefing(authorization="Bearer x")
        assert briefing_permission_spy[-1] == ("Bearer x", "view_founder_briefing")

    @pytest.mark.anyio
    async def test_no_stripe_revenue_is_honestly_none_with_note(self, briefing_permission_spy, monkeypatch):
        _empty_supabase(monkeypatch)
        result = await briefing_module.founder_daily_briefing(authorization="Bearer x")
        assert result["business"]["revenue_today"] is None
        assert "Stripe" in result["business"]["revenue_today_note"]
        assert result["business"]["premium_sales"] is None
        assert result["users"]["new_premium"] is None
        assert result["users"]["cancellations"] is None

    @pytest.mark.anyio
    async def test_ai_cost_and_errors_are_honestly_none(self, briefing_permission_spy, monkeypatch):
        _empty_supabase(monkeypatch)
        result = await briefing_module.founder_daily_briefing(authorization="Bearer x")
        assert result["ai"]["cost"] is None
        assert result["ai"]["errors"] is None
        assert result["ai"]["slow_responses"] is None

    @pytest.mark.anyio
    async def test_no_warnings_when_everything_is_fine(self, briefing_permission_spy, monkeypatch):
        monkeypatch.setattr(
            briefing_module,
            "supabase",
            _FakeBriefingSupabase(
                {
                    "vt_users": [{"email": "a@example.com", "created_at": "2000-01-01"}],
                    "vt_affiliate_products": [{"id": "p1", "status": "active", "link_status": "ok", "created_at": "2000-01-01"}],
                }
            ),
        )
        result = await briefing_module.founder_daily_briefing(authorization="Bearer x")
        assert result["warnings"] == []
        assert result["priorities"] == [{"label": "Keine dringenden Prioritäten", "priority": "niedrig"}]

    @pytest.mark.anyio
    async def test_broken_links_trigger_high_priority_warning(self, briefing_permission_spy, monkeypatch):
        monkeypatch.setattr(
            briefing_module,
            "supabase",
            _FakeBriefingSupabase(
                {
                    "vt_affiliate_products": [
                        {"id": "p1", "status": "active", "link_status": "broken", "created_at": "2000-01-01"},
                        {"id": "p2", "status": "active", "link_status": "broken", "created_at": "2000-01-01"},
                    ],
                }
            ),
        )
        result = await briefing_module.founder_daily_briefing(authorization="Bearer x")
        assert any("defekt" in w for w in result["warnings"])
        assert {"label": "Defekte Affiliate-Links beheben", "priority": "hoch"} in result["priorities"]
        assert any("2 Link(s) funktionieren nicht." in r["text"] for r in result["recommendations"])

    @pytest.mark.anyio
    async def test_database_unreachable_is_high_priority(self, briefing_permission_spy, monkeypatch):
        class _RaisingQuery:
            def select(self, *a, count=None, **k):
                raise RuntimeError("db down")

        class _RaisingSupabase:
            def table(self, name):
                return _RaisingQuery()

        monkeypatch.setattr(briefing_module, "supabase", _RaisingSupabase())
        result = await briefing_module.founder_daily_briefing(authorization="Bearer x")
        assert result["system"]["database"] == "unreachable"
        assert "nicht erreichbar" in result["warnings"][0]
        assert {"label": "Datenbank-Erreichbarkeit prüfen", "priority": "hoch"} in result["priorities"]

    @pytest.mark.anyio
    async def test_top_products_are_computed_from_real_conversions(self, briefing_permission_spy, monkeypatch):
        monkeypatch.setattr(
            briefing_module,
            "supabase",
            _FakeBriefingSupabase(
                {
                    "vt_affiliate_products": [{"id": "p1", "title": "Bestes Produkt", "status": "active", "link_status": "ok", "created_at": "2000-01-01"}],
                    "vt_affiliate_events": [
                        {"product_id": "p1", "event_type": "conversion", "revenue": 50.0},
                        {"product_id": "p1", "event_type": "conversion", "revenue": 10.0},
                    ],
                }
            ),
        )
        result = await briefing_module.founder_daily_briefing(authorization="Bearer x")
        assert result["affiliate"]["top_products"][0]["title"] == "Bestes Produkt"
        assert result["affiliate"]["top_products"][0]["revenue"] == 60.0

    @pytest.mark.anyio
    async def test_tasks_list_has_five_entries_matching_spec(self, briefing_permission_spy, monkeypatch):
        _empty_supabase(monkeypatch)
        result = await briefing_module.founder_daily_briefing(authorization="Bearer x")
        labels = [t["label"] for t in result["tasks"]]
        assert labels == ["Produkte prüfen", "Releases prüfen", "Bugs prüfen", "Support prüfen", "Dokumentation prüfen"]
        assert result["tasks"][1]["value"] is None and result["tasks"][1]["note"]
        assert result["tasks"][2]["value"] is None and result["tasks"][2]["note"]
        assert result["tasks"][4]["value"] is None and result["tasks"][4]["note"]
