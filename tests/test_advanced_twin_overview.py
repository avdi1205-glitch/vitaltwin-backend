"""Unit tests for the Pro feature "Erweiterter digitaler Zwilling V1"
(Advanced Twin Overview) — `services/advanced_twin_overview.py` and
`app.routers.profile::get_advanced_twin_overview`. Mocks Supabase and the
entitlement lookup — no real network/database access.

Constitution rule 17: this composes ALREADY-COMPUTED outputs from
`personal_baseline.py`/`thirty_day_report.py`/`trends.py` — no new
statistics engine. Free/Premium are blocked server-side (403); Pro/Family
get the real composed overview."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import profile as profile_module
from app.services.advanced_twin_overview import DISCLAIMER, build_advanced_twin_overview


def _entries_for_days(field: str, days: int, value: float = 7.0) -> list[dict[str, object]]:
    today = date.today()
    return [{"entry_date": (today - timedelta(days=i)).isoformat(), field: value} for i in range(days)]


class TestBuildAdvancedTwinOverview:
    def test_no_data_returns_an_honest_unavailable_state(self):
        overview = build_advanced_twin_overview(
            entries=[], habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert overview.available is False
        assert overview.data_points == 0
        assert overview.data_quality_overview == "keine Daten"
        assert overview.current_trends == {}
        assert overview.thirty_day_development["available"] is False
        # Lifestyle simulation entry point is always shown, even with no data.
        assert overview.lifestyle_simulation["available"] is True

    def test_partial_data_marks_low_quality_and_keeps_thirty_day_section_unavailable(self):
        entries = _entries_for_days("sleep_hours", days=5)
        overview = build_advanced_twin_overview(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert overview.available is True
        assert overview.data_quality_overview == "gering"
        # Below monthly_progress's own 10-day threshold — must stay honest, not fabricated.
        assert overview.thirty_day_development["available"] is False

    def test_full_data_produces_a_complete_overview(self):
        entries = _entries_for_days("sleep_hours", days=30)
        overview = build_advanced_twin_overview(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert overview.available is True
        assert overview.data_quality_overview == "ausreichend"
        assert "sleep_hours" in overview.current_trends
        assert overview.thirty_day_development["available"] is True
        assert overview.disclaimer == DISCLAIMER

    def test_missing_optional_sections_stay_empty_not_fabricated(self):
        entries = _entries_for_days("sleep_hours", days=30)
        overview = build_advanced_twin_overview(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert overview.active_goals == []
        assert overview.habit_progress == []
        assert overview.current_trends["movement_minutes"]["average"] is None

    def test_active_goals_and_habit_progress_reflect_real_input(self):
        entries = _entries_for_days("sleep_hours", days=30)
        goals = [{"title": "Mehr schlafen", "status": "active"}, {"title": "Altes Ziel", "status": "archived"}]
        habits = [{"name": "Laufen", "completion_rate_7d": 0.6}]
        overview = build_advanced_twin_overview(
            entries=entries, habits=habits, goals=goals, confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert any("Mehr schlafen" in note for note in overview.active_goals)
        assert not any("Altes Ziel" in note for note in overview.active_goals)
        assert any("Laufen" in note for note in overview.habit_progress)

    def test_biomarker_is_missing_by_default_even_in_zero_checkin_branch(self):
        overview = build_advanced_twin_overview(
            entries=[], habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today(),
        )
        assert overview.available is False  # existing zero-checkin contract unchanged
        assert overview.biomarker == {
            "available": False, "biologisches_alter": None, "differenz": None,
            "markers_provided": [], "last_updated": None,
            "reason": "Noch keine Twin-Berechnung durchgeführt.",
        }

    def test_biomarker_is_populated_independently_of_the_checkin_gate(self):
        # A user can have real biomarker calculations with ZERO check-ins —
        # biomarker must still surface in the zero-checkin early-return branch.
        calc = {
            "created_at": "2026-01-01T08:00:00+00:00", "biologisches_alter": 35.0, "differenz": -5.0,
            "scenarios": {"aktuell": 35.0}, "marker_breakdown": [{"marker": "hba1c", "value": 5.0, "contribution": -0.1}],
        }
        overview = build_advanced_twin_overview(
            entries=[], habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today(),
            biomarker_calculations=[calc],
        )
        assert overview.available is False  # 12 pre-existing keys/behavior untouched
        assert overview.biomarker["available"] is True
        assert overview.biomarker["biologisches_alter"] == 35.0

    def test_biomarker_is_additive_in_the_normal_data_branch_too(self):
        entries = _entries_for_days("sleep_hours", 15)
        calc = {
            "created_at": "2026-01-01T08:00:00+00:00", "biologisches_alter": 35.0, "differenz": -5.0,
            "scenarios": {"aktuell": 35.0}, "marker_breakdown": [{"marker": "hba1c", "value": 5.0, "contribution": -0.1}],
        }
        overview = build_advanced_twin_overview(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today(),
            biomarker_calculations=[calc],
        )
        assert overview.available is True
        assert overview.biomarker["biologisches_alter"] == 35.0


class _RecordingQuery:
    def __init__(self, calls_log, data=None):
        self._calls_log = calls_log
        self._data = data if data is not None else []
        self._state: dict[str, object] = {}

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._state[field] = value
        return self

    def in_(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, value):
        self._state["limit"] = value
        return self

    def execute(self):
        self._calls_log.append(dict(self._state))
        return SimpleNamespace(data=self._data)


class _RecordingSupabase:
    def __init__(self, data=None):
        self.calls: list[dict[str, object]] = []
        self._data = data

    def table(self, name):
        return _RecordingQuery(self.calls, self._data)


class _TableAwareQuery:
    """Unlike `_RecordingQuery` above (shared data across all tables), this
    fake filters per-table and per-`eq()` key — needed to prove the new
    biomarker fetch is genuinely scoped to the requesting user's own email
    and doesn't leak another user's `vt_twin_calculations` rows."""

    def __init__(self, rows: list[dict[str, object]]):
        self._all_rows = rows
        self._filters: dict[str, object] = {}

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    def in_(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, value):
        return self

    def execute(self):
        rows = [r for r in self._all_rows if all(r.get(k) == v for k, v in self._filters.items())]
        return SimpleNamespace(data=rows)


class _TableAwareSupabase:
    def __init__(self, tables: dict[str, list[dict[str, object]]]):
        self._tables = tables

    def table(self, name):
        return _TableAwareQuery(self._tables.get(name, []))


class TestAdvancedTwinOverviewBiomarkerIntegration:
    @pytest.mark.anyio
    async def test_biomarker_flows_through_the_endpoint_additively(self, monkeypatch):
        calc = {
            "email": "pro@example.com", "created_at": "2026-01-01T08:00:00+00:00",
            "biologisches_alter": 35.0, "differenz": -5.0, "scenarios": {}, "marker_breakdown": [],
        }
        fake = _TableAwareSupabase({"vt_twin_calculations": [calc]})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "pro@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.get_advanced_twin_overview(authorization="Bearer x")
        # every pre-existing key from the 12-key contract must still be present
        for key in (
            "available", "data_points", "data_quality_overview", "reason", "current_trends", "personal_baseline",
            "thirty_day_development", "active_goals", "habit_progress", "lifestyle_simulation",
            "twin_status_summary", "disclaimer",
        ):
            assert key in result
        assert result["biomarker"]["biologisches_alter"] == 35.0

    @pytest.mark.anyio
    async def test_biomarker_fetch_is_isolated_per_user(self, monkeypatch):
        rows = [
            {"email": "user-a@example.com", "created_at": "2026-01-01T08:00:00+00:00", "biologisches_alter": 30.0, "differenz": -10.0, "scenarios": {}, "marker_breakdown": []},
            {"email": "user-b@example.com", "created_at": "2026-01-01T08:00:00+00:00", "biologisches_alter": 99.0, "differenz": 50.0, "scenarios": {}, "marker_breakdown": []},
        ]
        fake = _TableAwareSupabase({"vt_twin_calculations": rows})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.get_advanced_twin_overview(authorization="Bearer x")
        assert result["biomarker"]["biologisches_alter"] == 30.0


class TestAdvancedTwinOverviewEndpointEntitlement:
    @pytest.mark.anyio
    async def test_free_user_is_denied(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.get_advanced_twin_overview(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_premium_user_is_denied(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "premium@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.get_advanced_twin_overview(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_pro_user_is_allowed(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "pro@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.get_advanced_twin_overview(authorization="Bearer x")
        assert result["available"] is False  # no data seeded, but no 403 — request succeeded
        assert result["disclaimer"] == DISCLAIMER

    @pytest.mark.anyio
    async def test_family_user_is_allowed(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "family@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.get_advanced_twin_overview(authorization="Bearer x")
        assert "reason" in result

    @pytest.mark.anyio
    async def test_entitlement_check_is_scoped_to_the_requesting_users_own_email(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")

        seen_emails: list[str] = []

        def fake_has_feature(email: str, feature: str) -> bool:
            seen_emails.append(email)
            return True

        monkeypatch.setattr(profile_module, "has_feature", fake_has_feature)
        await profile_module.get_advanced_twin_overview(authorization="Bearer x")
        assert seen_emails == ["user-a@example.com"]
