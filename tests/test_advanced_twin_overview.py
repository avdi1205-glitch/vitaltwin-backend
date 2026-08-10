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
