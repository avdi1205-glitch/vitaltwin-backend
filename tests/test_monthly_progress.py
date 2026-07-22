"""Unit tests for the Monthly Progress foundation
(`app.services.monthly_progress`). Pure functions — no network/database
access."""

from __future__ import annotations

from datetime import date

from app.services import monthly_progress

TODAY = date(2026, 7, 22)


class TestLockedWithoutEnoughData:
    def test_few_checkin_days_yields_unavailable(self):
        entries = [{"entry_date": "2026-07-20", "sleep_hours": 7}]
        result = monthly_progress.prepare_monthly_progress(
            daily_entries=entries,
            habits=[],
            goals=[],
            confirmed_memories=[],
            confirmed_patterns=[],
            today=TODAY,
        )
        assert result.available is False
        assert result.reason is not None
        assert result.thirty_day_trends == {}


class TestAvailableWithEnoughData:
    def _entries(self, count: int) -> list[dict]:
        return [{"entry_date": f"2026-07-{(i % 28) + 1:02d}", "sleep_hours": 7.0, "energy": 6} for i in range(count)]

    def test_enough_checkin_days_yields_available(self):
        result = monthly_progress.prepare_monthly_progress(
            daily_entries=self._entries(monthly_progress.MIN_CHECKIN_DAYS_FOR_MONTHLY),
            habits=[],
            goals=[],
            confirmed_memories=[],
            confirmed_patterns=[],
            today=TODAY,
        )
        assert result.available is True
        assert "sleep_hours" in result.thirty_day_trends

    def test_goal_and_habit_summaries_are_included(self):
        goals = [{"title": "Mehr Energie", "status": "active"}]
        habits = [{"name": "Laufen", "completion_rate_30d": 0.75}]
        result = monthly_progress.prepare_monthly_progress(
            daily_entries=self._entries(monthly_progress.MIN_CHECKIN_DAYS_FOR_MONTHLY),
            habits=habits,
            goals=goals,
            confirmed_memories=[],
            confirmed_patterns=[],
            today=TODAY,
        )
        assert any("Mehr Energie" in note for note in result.goal_development)
        assert any("Laufen" in note for note in result.habit_summary)

    def test_confirmed_preference_memories_are_listed_as_changed_preferences(self):
        memories = [
            {"memory_type": "bestaetigte_praeferenz", "human_readable_value": "Bevorzugt Schlaf-Empfehlungen"},
            {"memory_type": "erfolgreiche_routine", "human_readable_value": "Sollte nicht auftauchen"},
        ]
        result = monthly_progress.prepare_monthly_progress(
            daily_entries=self._entries(monthly_progress.MIN_CHECKIN_DAYS_FOR_MONTHLY),
            habits=[],
            goals=[],
            confirmed_memories=memories,
            confirmed_patterns=[],
            today=TODAY,
        )
        assert result.changed_preferences == ["Bevorzugt Schlaf-Empfehlungen"]

    def test_memory_development_counts_by_status(self):
        memories = [
            {"memory_type": "bestaetigte_praeferenz", "status": "confirmed"},
            {"memory_type": "erfolgreiche_routine", "status": "confirmed"},
            {"memory_type": "aktives_langfristiges_ziel", "status": "active"},
        ]
        result = monthly_progress.prepare_monthly_progress(
            daily_entries=self._entries(monthly_progress.MIN_CHECKIN_DAYS_FOR_MONTHLY),
            habits=[],
            goals=[],
            confirmed_memories=memories,
            confirmed_patterns=[],
            today=TODAY,
        )
        assert result.memory_development == {"confirmed": 2, "active": 1}
