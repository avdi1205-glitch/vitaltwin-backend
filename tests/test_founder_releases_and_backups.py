"""Unit tests for `app.core.founder_releases` and
`app.core.founder_backup_status` (Founder OS internal foundations #5 + #6).
Mocks Supabase — no real network. Both tables start EMPTY: these tests
assert that "no data yet" honestly returns `None`, never a fabricated row."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import founder_backup_status, founder_releases


class _FakeQuery:
    def __init__(self, store: list):
        self._store = store

    def insert(self, payload):
        row = dict(payload)
        row["id"] = len(self._store) + 1
        self._store.append(row)
        return self

    def select(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, n=None):
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._store))


class _FakeSupabase:
    def __init__(self):
        self.rows: list = []

    def table(self, name):
        return _FakeQuery(self.rows)


@pytest.fixture
def fake_release_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(founder_releases, "supabase", fake)
    return fake


@pytest.fixture
def fake_backup_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(founder_backup_status, "supabase", fake)
    return fake


class TestFounderReleases:
    def test_get_latest_release_none_when_empty(self, fake_release_supabase):
        assert founder_releases.get_latest_release() is None

    def test_record_and_fetch_latest_release(self, fake_release_supabase):
        result = founder_releases.record_release(version="1.2.0", released_by="founder@example.com")
        assert result["version"] == "1.2.0"
        assert result["build_status"] == "unbekannt"
        latest = founder_releases.get_latest_release()
        assert latest["version"] == "1.2.0"

    def test_invalid_build_status_falls_back_to_unbekannt(self, fake_release_supabase):
        result = founder_releases.record_release(version="1.0.0", build_status="not_a_real_status")
        assert result["build_status"] == "unbekannt"

    def test_record_release_returns_none_on_failure(self, monkeypatch):
        class _Broken:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(founder_releases, "supabase", _Broken())
        assert founder_releases.record_release(version="1.0.0") is None


class TestFounderBackupStatus:
    def test_get_latest_backup_none_when_empty(self, fake_backup_supabase):
        assert founder_backup_status.get_latest_backup_status() is None

    def test_record_and_fetch_latest_backup(self, fake_backup_supabase):
        result = founder_backup_status.record_backup(status="erfolgreich", recorded_by="founder@example.com")
        assert result["status"] == "erfolgreich"
        latest = founder_backup_status.get_latest_backup_status()
        assert latest["status"] == "erfolgreich"

    def test_invalid_status_is_rejected(self, fake_backup_supabase):
        assert founder_backup_status.record_backup(status="not_a_real_status") is None

    def test_record_backup_returns_none_on_failure(self, monkeypatch):
        class _Broken:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(founder_backup_status, "supabase", _Broken())
        assert founder_backup_status.record_backup(status="erfolgreich") is None
