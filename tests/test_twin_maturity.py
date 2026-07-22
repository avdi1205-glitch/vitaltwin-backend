"""Unit tests for the Twin-Reifegrad (`app.services.twin_maturity`). Pure
functions — no network/database access."""

from __future__ import annotations

from app.services import twin_maturity


BASE_KWARGS = dict(
    checkin_day_count=0,
    account_age_days=0,
    confirmed_memory_count=0,
    has_routine_or_time_memory=False,
    has_confirmed_preference=False,
    has_active_pattern=False,
    weekly_reflection_count=0,
)


class TestMaturityLevels:
    def test_no_data_yields_start(self):
        result = twin_maturity.compute_twin_maturity(**BASE_KWARGS)
        assert result.level == "start"
        assert result.missing_data

    def test_enough_checkins_yields_lernt_dich_kennen(self):
        kwargs = {**BASE_KWARGS, "checkin_day_count": twin_maturity.MIN_CHECKIN_DAYS_LEARNING}
        result = twin_maturity.compute_twin_maturity(**kwargs)
        assert result.level == "lernt_dich_kennen"

    def test_routine_and_age_yields_erkennt_routinen(self):
        kwargs = {
            **BASE_KWARGS,
            "checkin_day_count": twin_maturity.MIN_CHECKIN_DAYS_LEARNING,
            "account_age_days": twin_maturity.MIN_ACCOUNT_AGE_ROUTINES_DAYS,
            "has_routine_or_time_memory": True,
        }
        result = twin_maturity.compute_twin_maturity(**kwargs)
        assert result.level == "erkennt_routinen"

    def test_pattern_alone_also_yields_erkennt_routinen(self):
        kwargs = {
            **BASE_KWARGS,
            "checkin_day_count": twin_maturity.MIN_CHECKIN_DAYS_LEARNING,
            "account_age_days": twin_maturity.MIN_ACCOUNT_AGE_ROUTINES_DAYS,
            "has_active_pattern": True,
        }
        result = twin_maturity.compute_twin_maturity(**kwargs)
        assert result.level == "erkennt_routinen"

    def test_confirmed_preference_yields_versteht_praeferenzen(self):
        kwargs = {
            **BASE_KWARGS,
            "checkin_day_count": twin_maturity.MIN_CHECKIN_DAYS_LEARNING,
            "account_age_days": twin_maturity.MIN_ACCOUNT_AGE_PREFERENCES_DAYS,
            "has_routine_or_time_memory": True,
            "has_confirmed_preference": True,
            "confirmed_memory_count": twin_maturity.MIN_CONFIRMED_MEMORIES_PREFERENCES,
        }
        result = twin_maturity.compute_twin_maturity(**kwargs)
        assert result.level == "versteht_praeferenzen"

    def test_full_history_yields_begleitet_langfristig(self):
        kwargs = {
            **BASE_KWARGS,
            "checkin_day_count": twin_maturity.MIN_CHECKIN_DAYS_LEARNING,
            "account_age_days": twin_maturity.MIN_ACCOUNT_AGE_LONGTERM_DAYS,
            "has_routine_or_time_memory": True,
            "has_confirmed_preference": True,
            "confirmed_memory_count": twin_maturity.MIN_CONFIRMED_MEMORIES_LONGTERM,
            "weekly_reflection_count": twin_maturity.MIN_WEEKLY_REFLECTIONS_LONGTERM,
        }
        result = twin_maturity.compute_twin_maturity(**kwargs)
        assert result.level == "begleitet_langfristig"
        assert result.missing_data == []

    def test_missing_data_explains_gap_to_next_level(self):
        result = twin_maturity.compute_twin_maturity(**BASE_KWARGS)
        assert any("Check-in-Tage" in note for note in result.missing_data)

    def test_present_data_reflects_inputs(self):
        kwargs = {**BASE_KWARGS, "checkin_day_count": 3}
        result = twin_maturity.compute_twin_maturity(**kwargs)
        assert result.present_data["checkin_day_count"] == 3
