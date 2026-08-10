"""Unit tests for the Pro feature "Erweiterte Berichte" (30-Day Wellness
Report) — `services/thirty_day_report.py` and
`app.routers.profile::get_thirty_day_report`. Mocks Supabase and the
entitlement lookup — no real network/database access.

Constitution rule 10: the report never claims causality between metrics.
Constitution rule 6: every result distinguishes measured/calculated data
from missing/insufficient data. Free/Premium are blocked server-side (403);
Pro/Family get the real report assembled from existing Monthly Progress +
Personal Baseline services (no duplicate analytics engine)."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import profile as profile_module
from app.services.thirty_day_report import (
    DISCLAIMER,
    MIN_DAYS_FOR_FULL_REPORT,
    build_thirty_day_report,
)


def _entries_for_days(field: str, values_by_days_ago: dict[int, float]) -> list[dict[str, object]]:
    today = date.today()
    return [
        {"entry_date": (today - timedelta(days=days_ago)).isoformat(), field: value}
        for days_ago, value in values_by_days_ago.items()
    ]


class TestBuildThirtyDayReport:
    def test_no_history_returns_insufficient_data_state(self):
        report = build_thirty_day_report(
            entries=[], habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert report.available is False
        assert report.data_points == 0
        assert report.reason is not None
        assert report.trends == {}
        assert report.strongest_positive_trend is None
        assert report.strongest_negative_trend is None

    def test_partial_history_below_threshold_stays_insufficient(self):
        entries = _entries_for_days("sleep_hours", {i: 7.0 for i in range(MIN_DAYS_FOR_FULL_REPORT - 1)})
        report = build_thirty_day_report(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert report.available is False
        assert report.data_points == MIN_DAYS_FOR_FULL_REPORT - 1

    def test_full_thirty_day_history_produces_an_available_report(self):
        entries = _entries_for_days("sleep_hours", {i: 7.0 for i in range(30)})
        report = build_thirty_day_report(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert report.available is True
        assert report.data_points == 30
        assert report.coverage_ratio == 1.0
        assert "sleep_hours" in report.trends
        assert report.disclaimer == DISCLAIMER

    def test_missing_optional_metrics_do_not_break_the_report(self):
        """Movement/stress entirely absent — report must still assemble,
        just without a highlight for those fields (never a fake number)."""
        entries = _entries_for_days("sleep_hours", {i: 7.0 for i in range(15)})
        report = build_thirty_day_report(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert report.available is True
        assert report.trends["movement_minutes"]["average"] is None
        assert report.trends["stress"]["average"] is None

    def test_strongest_positive_trend_uses_the_real_first_vs_second_half_delta(self):
        # First 15-day half (older): low movement. Second half (recent): higher.
        entries = _entries_for_days("movement_minutes", {**{i: 10.0 for i in range(15, 30)}, **{i: 40.0 for i in range(15)}})
        report = build_thirty_day_report(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert report.strongest_positive_trend is not None
        assert report.strongest_positive_trend["field"] == "movement_minutes"
        assert report.strongest_positive_trend["first_half_average"] == 10.0
        assert report.strongest_positive_trend["second_half_average"] == 40.0

    def test_strongest_negative_trend_accounts_for_stress_being_inverted(self):
        # Stress rising is a NEGATIVE development (higher_is_better=False).
        entries = _entries_for_days("stress", {**{i: 2.0 for i in range(15, 30)}, **{i: 8.0 for i in range(15)}})
        report = build_thirty_day_report(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert report.strongest_negative_trend is not None
        assert report.strongest_negative_trend["field"] == "stress"

    def test_summary_never_claims_causality_between_metrics(self):
        entries = _entries_for_days("sleep_hours", {**{i: 6.0 for i in range(15, 30)}, **{i: 8.0 for i in range(15)}})
        report = build_thirty_day_report(
            entries=entries, habits=[], goals=[], confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        forbidden_terms = ["verursacht", "deshalb", "dadurch", "führt zu"]
        assert not any(term in report.summary.lower() for term in forbidden_terms)

    def test_habit_and_goal_progress_pass_through_from_monthly_progress(self):
        entries = _entries_for_days("sleep_hours", {i: 7.0 for i in range(30)})
        habits = [{"name": "Laufen", "status": "active", "completion_rate_30d": 0.75}]
        goals = [{"title": "Mehr schlafen", "status": "active"}]
        report = build_thirty_day_report(
            entries=entries, habits=habits, goals=goals, confirmed_memories=[], confirmed_patterns=[], today=date.today()
        )
        assert any("Laufen" in note for note in report.habit_progress)
        assert any("Mehr schlafen" in note for note in report.goal_progress)


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


class TestThirtyDayReportEndpointEntitlement:
    @pytest.mark.anyio
    async def test_free_user_is_denied(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.get_thirty_day_report(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_premium_user_is_denied(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "premium@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.get_thirty_day_report(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_pro_user_is_allowed(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "pro@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.get_thirty_day_report(authorization="Bearer x")
        assert result["available"] is False  # no data seeded, but no 403 — request succeeded
        assert result["disclaimer"] == DISCLAIMER

    @pytest.mark.anyio
    async def test_family_user_is_allowed(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "family@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.get_thirty_day_report(authorization="Bearer x")
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
        await profile_module.get_thirty_day_report(authorization="Bearer x")
        assert seen_emails == ["user-a@example.com"]
