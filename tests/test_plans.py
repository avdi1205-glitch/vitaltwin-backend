"""Unit tests for `app.core.plans` tariff limit helpers (Etappe 7 §7)."""

from __future__ import annotations

import pytest

from app.core.plans import get_chat_daily_limit, get_context_char_limit


class TestChatDailyLimit:
    @pytest.mark.parametrize("plan,expected", [("free", 3), ("premium", 30), ("pro", 60), ("family", 30)])
    def test_known_plans_have_expected_limits(self, plan, expected):
        assert get_chat_daily_limit(plan) == expected

    def test_unknown_plan_falls_back_to_free(self):
        assert get_chat_daily_limit("not-a-real-plan") == get_chat_daily_limit("free")


class TestContextCharLimit:
    def test_free_has_the_smallest_context(self):
        limits = [get_context_char_limit(p) for p in ("free", "premium", "pro")]
        assert limits == sorted(limits)
        assert get_context_char_limit("free") < get_context_char_limit("premium")
        assert get_context_char_limit("premium") < get_context_char_limit("pro")

    def test_family_matches_premium_until_distinguishable_in_db(self):
        assert get_context_char_limit("family") == get_context_char_limit("premium")

    def test_unknown_plan_falls_back_to_free(self):
        assert get_context_char_limit("not-a-real-plan") == get_context_char_limit("free")
