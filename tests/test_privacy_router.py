"""Unit tests for `app.routers.privacy` — Pydantic validation, category
handling, email-scoping (Etappe 9 §1/§2: "keine Daten anderer Nutzer"), and
audit-event firing. Mocks Supabase and auth — no real network/database
access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import privacy as privacy_module
from app.routers.privacy import CATEGORY_TABLES, ConsentInput


class _RecordingQuery:
    def __init__(self, table_name: str, calls_log: list[tuple[str, str, object]], data=None):
        self._table_name = table_name
        self._calls_log = calls_log
        self._data = data if data is not None else []

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._calls_log.append((self._table_name, field, value))
        return self

    def order(self, *args, **kwargs):
        return self

    def delete(self):
        return self

    def insert(self, *args, **kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _RecordingSupabase:
    def __init__(self, data=None):
        self.calls: list[tuple[str, str, object]] = []
        self._data = data

    def table(self, name):
        return _RecordingQuery(name, self.calls, self._data)


class TestConsentInput:
    @pytest.mark.parametrize(
        "consent_type",
        [
            "wellness_data_processing",
            "ai_features",
            "chat_storage",
            "wearables_future",
            "marketing",
            "affiliate_tracking",
            "research_optional",
        ],
    )
    def test_accepts_all_seven_purposes(self, consent_type):
        model = ConsentInput(consent_type=consent_type, granted=True)
        assert model.consent_type == consent_type

    def test_rejects_unknown_purpose(self):
        with pytest.raises(Exception):
            ConsentInput(consent_type="everything", granted=True)


class TestCategoryTables:
    def test_all_twelve_categories_are_defined(self):
        expected = {
            "checkins", "habits", "habit_entries", "goals", "daily_plans", "reflections",
            "weekly_reflections", "recommendations", "memories", "patterns", "chat_history", "feedback",
        }
        assert set(CATEGORY_TABLES.keys()) == expected


class TestLoadCategoryRowsScoping:
    def test_every_category_query_is_scoped_by_email(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(privacy_module, "supabase", fake)

        email = "user-a@example.com"
        for category in CATEGORY_TABLES:
            privacy_module._load_category_rows(email, category)

        email_calls = [c for c in fake.calls if c[1] == "email"]
        assert all(value == email for _, _, value in email_calls)
        touched_tables = {table for table, _, _ in email_calls}
        assert touched_tables == set(CATEGORY_TABLES.values())


class TestDeleteDataCategory:
    @pytest.mark.anyio
    async def test_invalid_category_raises_422(self, monkeypatch):
        monkeypatch.setattr(privacy_module, "_require_email", lambda auth: "user-a@example.com")
        with pytest.raises(HTTPException) as exc_info:
            await privacy_module.delete_data_category("not-a-real-category", authorization="Bearer x")
        assert exc_info.value.status_code == 422

    @pytest.mark.anyio
    async def test_valid_category_deletes_and_records_audit_event(self, monkeypatch):
        fake = _RecordingSupabase(data=[{"id": "1"}, {"id": "2"}])
        monkeypatch.setattr(privacy_module, "supabase", fake)
        monkeypatch.setattr(privacy_module, "_require_email", lambda auth: "user-a@example.com")

        recorded_events = []
        monkeypatch.setattr(
            privacy_module,
            "record_audit_event",
            lambda **kwargs: recorded_events.append(kwargs),
        )

        result = await privacy_module.delete_data_category("habit_entries", authorization="Bearer x")
        assert result["deleted_count"] == 2
        assert len(recorded_events) == 1
        assert recorded_events[0]["action"] == "delete"
        assert recorded_events[0]["entity_type"] == "category:habit_entries"


class TestSetConsent:
    @pytest.mark.anyio
    async def test_records_consent_change_audit_event(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(privacy_module, "supabase", fake)
        monkeypatch.setattr(privacy_module, "_require_email", lambda auth: "user-a@example.com")

        recorded_events = []
        monkeypatch.setattr(
            privacy_module,
            "record_audit_event",
            lambda **kwargs: recorded_events.append(kwargs),
        )

        data = ConsentInput(consent_type="marketing", granted=False)
        result = await privacy_module.set_consent(data, authorization="Bearer x")
        assert result["granted"] is False
        assert recorded_events[0]["action"] == "consent_change"
        assert recorded_events[0]["entity_id"] == "marketing"


@pytest.fixture
def anyio_backend():
    return "asyncio"
