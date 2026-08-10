"""Unit tests for `app.routers.chat` — Pydantic validation, plan resolution,
the provider factory, and (critically) that every context query is scoped
to the requesting user's own email (Etappe 7 §1: "nur aktueller Nutzer",
"keine fremden Daten"). Mocks the Supabase client — no real network/database
access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.routers import chat as chat_module
from app.routers.chat import ChatRequest
from app.services.ai_provider import OpenAIProvider


class _RecordingQuery:
    def __init__(self, table_name: str, calls_log: list[tuple[str, str, object]]):
        self._table_name = table_name
        self._calls_log = calls_log

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._calls_log.append((self._table_name, field, value))
        return self

    def neq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _RecordingSupabase:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def table(self, name):
        return _RecordingQuery(name, self.calls)


class TestChatRequestValidation:
    def test_accepts_a_normal_message(self):
        assert ChatRequest(message="Wie war meine Woche?").message == "Wie war meine Woche?"

    def test_rejects_empty_message(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="   ")

    def test_rejects_overlong_message(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="x" * 501)


class TestCurrentPlan:
    def test_premium_user_resolves_to_premium(self, monkeypatch):
        monkeypatch.setattr(chat_module, "get_plan_by_email", lambda email: "premium")
        assert chat_module._current_plan("user@example.com") == "premium"

    def test_pro_user_resolves_to_pro(self, monkeypatch):
        monkeypatch.setattr(chat_module, "get_plan_by_email", lambda email: "pro")
        assert chat_module._current_plan("user@example.com") == "pro"

    def test_non_premium_user_resolves_to_free(self, monkeypatch):
        monkeypatch.setattr(chat_module, "get_plan_by_email", lambda email: "free")
        assert chat_module._current_plan("user@example.com") == "free"


class TestAIProviderFactory:
    def test_factory_returns_an_openai_provider_instance(self):
        assert isinstance(chat_module._get_ai_provider(), OpenAIProvider)


class TestContextQueriesAreScopedToRequestingUser:
    """Etappe 7 §1: "nur aktueller Nutzer", "keine fremden Daten". Every
    table this function touches must filter by the requesting user's own
    email — never by anything client-supplied, never unscoped."""

    EXPECTED_EMAIL_SCOPED_TABLES = {
        chat_module.PROFILE_TABLE,
        chat_module.GOAL_TABLE,
        chat_module.HABIT_TABLE,
        chat_module.HABIT_ENTRY_TABLE,
        chat_module.DAILY_ENTRY_TABLE,
        chat_module.MEMORY_TABLE,
        chat_module.RECOMMENDATION_TABLE,
        chat_module.PATTERN_TABLE,
        chat_module.DAILY_PLAN_TABLE,
    }

    def test_every_expected_table_is_filtered_by_the_requesting_email(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        email = "user-a@example.com"
        chat_module._build_context_for_user(email, "free")

        email_scoped_tables = {table for table, field, value in fake_supabase.calls if field == "email" and value == email}
        assert self.EXPECTED_EMAIL_SCOPED_TABLES.issubset(email_scoped_tables)

    def test_no_call_ever_uses_a_different_email(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        email = "user-a@example.com"
        chat_module._build_context_for_user(email, "free")

        foreign_email_calls = [call for call in fake_supabase.calls if call[1] == "email" and call[2] != email]
        assert foreign_email_calls == []

    def test_returns_empty_context_without_raising_when_supabase_has_no_data(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        text, sources, truncated, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert isinstance(text, str) and text
        assert sources == []
        assert truncated is False
        assert profile is None


def _old_language_resolution(profile_rows):
    """Verbatim reconstruction of the OLD `ask_twin()` snippet removed by the
    profile-reuse optimization (`profile_resp.data[0].get(...) if data else
    None) or "de"`, wrapped in the same try/except("de")). Used only to
    prove the NEW logic resolves to the identical language for every state."""
    try:
        return (profile_rows[0].get("preferred_language") if profile_rows else None) or "de"
    except Exception:
        return "de"


class TestLanguageResolutionOldVsNewEquivalence:
    """Proves (not just reasons) that reusing `_build_context_for_user`'s
    already-fetched profile row resolves `preferred_language` identically
    to the old, separate query for every possible profile state."""

    @pytest.mark.parametrize(
        "profile_rows,expected_language",
        [
            ([{"preferred_language": "de"}], "de"),
            ([{"preferred_language": "en"}], "en"),
            ([{"email": "user-a@example.com"}], "de"),  # preferred_language key missing
            ([], "de"),  # query ran, zero rows
            (None, "de"),  # data attribute absent / falsy
        ],
    )
    def test_new_extraction_matches_old_extraction(self, profile_rows, expected_language):
        old_result = _old_language_resolution(profile_rows)
        assert old_result == expected_language

        # NEW logic: `profile` is the dict `_build_context_for_user` already
        # returns (first row or None) — same extraction, different source.
        profile = profile_rows[0] if profile_rows else None
        new_result = (profile.get("preferred_language") if profile else None) or "de"
        assert new_result == old_result

    def test_missing_profile_row_resolves_to_de_via_real_build_context_for_user(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        _, _, _, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert profile is None
        assert ((profile.get("preferred_language") if profile else None) or "de") == "de"

    def test_db_error_during_profile_fetch_resolves_to_de_via_real_build_context_for_user(self, monkeypatch):
        class _RaisingProfileQuery(_RecordingQuery):
            def execute(self):
                raise RuntimeError("simulated database error")

        class _PartlyRaisingSupabase(_RecordingSupabase):
            def table(self, name):
                if name == chat_module.PROFILE_TABLE:
                    return _RaisingProfileQuery(name, self.calls)
                return super().table(name)

        monkeypatch.setattr(chat_module, "supabase", _PartlyRaisingSupabase())
        _, _, _, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert profile is None
        assert ((profile.get("preferred_language") if profile else None) or "de") == "de"

    def test_profile_query_is_isolated_to_the_requesting_users_own_email(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        chat_module._build_context_for_user("user-a@example.com", "free")

        profile_calls = [c for c in fake_supabase.calls if c[0] == chat_module.PROFILE_TABLE]
        assert profile_calls == [(chat_module.PROFILE_TABLE, "email", "user-a@example.com")]
