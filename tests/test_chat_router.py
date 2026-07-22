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
        monkeypatch.setattr(chat_module, "is_premium_by_email", lambda email: True)
        assert chat_module._current_plan("user@example.com") == "premium"

    def test_non_premium_user_resolves_to_free(self, monkeypatch):
        monkeypatch.setattr(chat_module, "is_premium_by_email", lambda email: False)
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

        text, sources, truncated = chat_module._build_context_for_user("user-a@example.com", "free")
        assert isinstance(text, str) and text
        assert sources == []
        assert truncated is False
