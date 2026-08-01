"""Unit tests for `app.core.system_events` and `app.core.error_events`
(Founder OS internal foundations #3 + #7). Mocks Supabase — no real network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import error_events, system_events


class _FakeQuery:
    def __init__(self, store: list):
        self._store = store

    def insert(self, payload):
        self._store.append(payload)
        return self

    def select(self, *args, **kwargs):
        return self

    def gte(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._store), count=len(self._store))


class _FakeSupabase:
    def __init__(self):
        self.rows: list = []

    def table(self, name):
        return _FakeQuery(self.rows)


@pytest.fixture
def fake_system_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(system_events, "supabase", fake)
    return fake


@pytest.fixture
def fake_error_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(error_events, "supabase", fake)
    return fake


class TestLogSystemEvent:
    def test_logs_event_with_valid_severity(self, fake_system_supabase):
        system_events.log_system_event(event_type="server_start", message="Backend gestartet.", severity="info")
        assert fake_system_supabase.rows[0]["event_type"] == "server_start"
        assert fake_system_supabase.rows[0]["severity"] == "info"

    def test_falls_back_to_info_for_unknown_severity(self, fake_system_supabase):
        system_events.log_system_event(event_type="x", message="y", severity="not_a_real_severity")
        assert fake_system_supabase.rows[0]["severity"] == "info"

    def test_never_raises_on_failure(self, monkeypatch):
        class _Broken:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(system_events, "supabase", _Broken())
        system_events.log_system_event(event_type="x", message="y")

    def test_list_recent_system_events(self, fake_system_supabase):
        fake_system_supabase.rows.append({"event_type": "server_start"})
        items = system_events.list_recent_system_events(limit=5)
        assert len(items) == 1


class TestLogErrorEvent:
    def test_logs_error_with_source_and_type(self, fake_error_supabase):
        error_events.log_error_event(source="/api/twin/calculate", error_type="ValueError", message="boom")
        row = fake_error_supabase.rows[0]
        assert row["source"] == "/api/twin/calculate"
        assert row["error_type"] == "ValueError"

    def test_never_raises_on_failure(self, monkeypatch):
        class _Broken:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(error_events, "supabase", _Broken())
        error_events.log_error_event(source="x", error_type="y", message="z")

    def test_get_error_summary_groups_by_type(self, fake_error_supabase):
        fake_error_supabase.rows.extend([
            {"error_type": "ValueError", "created_at": "2026-01-01T00:00:00Z"},
            {"error_type": "ValueError", "created_at": "2026-01-01T00:00:00Z"},
            {"error_type": "KeyError", "created_at": "2026-01-01T00:00:00Z"},
        ])
        summary = error_events.get_error_summary(days=7)
        assert summary["total"] == 3
        assert summary["by_type"]["ValueError"] == 2
        assert summary["by_type"]["KeyError"] == 1

    def test_get_error_summary_honest_none_when_unreachable(self, monkeypatch):
        class _Broken:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(error_events, "supabase", _Broken())
        summary = error_events.get_error_summary(days=7)
        assert summary["total"] is None
        assert summary["note"]
