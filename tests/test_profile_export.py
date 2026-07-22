"""Unit tests for the Etappe 9 full-export extension of
`app.routers.profile::export_profile` and the deletion-request audit event.
Mocks Supabase and auth — no real network/database access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import profile as profile_module


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

    def limit(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def update(self, *args, **kwargs):
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


EXPECTED_EXPORT_KEYS = {
    "profile",
    "daily_wellness_entries",
    "habits",
    "habit_entries",
    "goals",
    "daily_plans",
    "daily_plan_actions",
    "daily_reflections",
    "weekly_reflections",
    "recommendations",
    "recommendation_decisions",
    "recommendation_outcomes",
    "recommendation_feedback",
    "twin_memories",
    "twin_patterns",
    "twin_learning_events",
    "consents",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TestExportProfileCompleteness:
    @pytest.mark.anyio
    async def test_bundle_contains_every_expected_category(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(profile_module, "record_audit_event", lambda **kwargs: None)

        result = await profile_module.export_profile(authorization="Bearer x")
        assert EXPECTED_EXPORT_KEYS.issubset(result.keys())
        assert result["email"] == "user-a@example.com"

    @pytest.mark.anyio
    async def test_every_query_is_scoped_to_requesting_email(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(profile_module, "record_audit_event", lambda **kwargs: None)

        await profile_module.export_profile(authorization="Bearer x")

        email_calls = [c for c in fake.calls if c[1] == "email"]
        assert email_calls  # at least one scoped query happened
        assert all(value == "user-a@example.com" for _, _, value in email_calls)

    @pytest.mark.anyio
    async def test_records_export_audit_event(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")

        recorded = []
        monkeypatch.setattr(profile_module, "record_audit_event", lambda **kwargs: recorded.append(kwargs))

        await profile_module.export_profile(authorization="Bearer x")
        assert recorded[0]["action"] == "export_request"
        assert recorded[0]["entity_type"] == "full_export"

    @pytest.mark.anyio
    async def test_oversized_export_is_rejected(self, monkeypatch):
        # 17 categories * 400 rows each comfortably exceeds MAX_SYNC_EXPORT_ROWS (5000).
        fake = _RecordingSupabase(data=[{"id": str(i)} for i in range(400)])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(profile_module, "record_audit_event", lambda **kwargs: None)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.export_profile(authorization="Bearer x")
        assert exc_info.value.status_code == 413


class TestRequestDeletionAuditEvent:
    @pytest.mark.anyio
    async def test_records_deletion_request_audit_event(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")

        recorded = []
        monkeypatch.setattr(profile_module, "record_audit_event", lambda **kwargs: recorded.append(kwargs))

        await profile_module.request_deletion(authorization="Bearer x")
        assert recorded[0]["action"] == "deletion_request"
        assert recorded[0]["entity_type"] == "account"
