"""Tests for Health Connect Phase 2 (steps -> existing Twin Core pipeline):
`core.health_normalization_service.normalize_health_connect_steps`,
`routers.health_connect`, and the 3-tier precedence extension in
`services.google_health_signals.resolve_trend_source`.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import CurrentUser
from app.core.health_normalization_service import normalize_health_connect_steps
from app.routers import health_connect as router_module
from app.services import google_health_signals as ghs


# ---------------------------------------------------------------------------
# normalize_health_connect_steps
# ---------------------------------------------------------------------------


class TestNormalizeHealthConnectSteps:
    def test_normalizes_a_real_shaped_record(self):
        row = normalize_health_connect_steps(
            {
                "id": "hc-record-1",
                "count": 1234,
                "startTime": "2026-08-13T08:00:00Z",
                "endTime": "2026-08-13T09:00:00Z",
            }
        )
        assert row is not None
        assert row["provider"] == "health_connect"
        assert row["provider_record_name"] == "hc-record-1"
        assert row["data_type"] == "steps"
        assert row["value"] == 1234.0
        assert row["unit"] == "count"
        assert row["source_name"] == "health_connect"
        assert row["start_time"] == "2026-08-13T08:00:00Z"
        assert row["end_time"] == "2026-08-13T09:00:00Z"

    def test_missing_start_time_returns_none(self):
        assert normalize_health_connect_steps({"id": "x", "count": 10, "startTime": ""}) is None

    def test_missing_count_returns_none(self):
        assert normalize_health_connect_steps({"id": "x", "startTime": "2026-08-13T08:00:00Z"}) is None

    def test_missing_id_still_normalizes_with_no_provider_record_name(self):
        row = normalize_health_connect_steps({"count": 5, "startTime": "2026-08-13T08:00:00Z"})
        assert row is not None
        assert row["provider_record_name"] is None


# ---------------------------------------------------------------------------
# In-memory Supabase fake (same conventions as test_google_health.py)
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: list[dict], id_counter: itertools.count):
        self._rows = rows
        self._id_counter = id_counter
        self._eq: list[tuple] = []
        self._op: str | None = None
        self._payload: dict | None = None
        self._on_conflict: str | None = None

    def select(self, *_a, **_k):
        self._op = self._op or "select"
        return self

    def eq(self, field, value):
        self._eq.append((field, value))
        return self

    def upsert(self, payload: dict, on_conflict: str | None = None):
        self._op = "upsert"
        self._payload = dict(payload)
        self._on_conflict = on_conflict
        return self

    def _matches(self, row: dict) -> bool:
        return all(row.get(field) == value for field, value in self._eq)

    def execute(self):
        if self._op == "upsert":
            conflict_fields = (self._on_conflict or "").split(",")
            existing = None
            if conflict_fields and conflict_fields[0]:
                for row in self._rows:
                    if all(row.get(f) == (self._payload or {}).get(f) for f in conflict_fields):
                        existing = row
                        break
            if existing is not None:
                existing.update(self._payload or {})
                return SimpleNamespace(data=[existing])
            row = dict(self._payload or {})
            row.setdefault("id", next(self._id_counter))
            self._rows.append(row)
            return SimpleNamespace(data=[row])
        matched = [r for r in self._rows if self._matches(r)]
        return SimpleNamespace(data=[dict(r) for r in matched])


class _FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self._id_counter = itertools.count(1)

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self.tables.setdefault(name, []), self._id_counter)


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(router_module, "supabase", fake, raising=False)
    return fake


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/health")
    monkeypatch.setattr(router_module, "has_feature", lambda email, feature: True)
    return TestClient(app)


def _auth_as(monkeypatch, user_id: int, email: str = "user@example.com"):
    monkeypatch.setattr(
        router_module, "require_user", lambda authorization: CurrentUser(email=email, user_id=user_id)
    )


# ---------------------------------------------------------------------------
# POST /health-connect/sync
# ---------------------------------------------------------------------------


class TestHealthConnectSyncEndpoint:
    def test_stores_records_with_health_connect_provider_and_no_connection(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z", "endTime": "2026-08-13T09:00:00Z"}]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"received": 1, "stored": 1, "skipped": 0, "debug_last_error": None}
        rows = fake_supabase.tables["health_activity_records"]
        assert len(rows) == 1
        assert rows[0]["provider"] == "health_connect"
        assert rows[0]["connection_id"] is None
        assert rows[0]["user_id"] == 42
        assert rows[0]["data_type"] == "steps"
        assert rows[0]["value"] == 500.0

    def test_resyncing_the_same_record_does_not_duplicate(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        payload = {"records": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z"}]}
        client.post("/api/health/health-connect/sync", json=payload, headers={"Authorization": "Bearer t"})
        client.post("/api/health/health-connect/sync", json=payload, headers={"Authorization": "Bearer t"})
        assert len(fake_supabase.tables["health_activity_records"]) == 1

    def test_two_users_do_not_see_each_others_rows(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=1)
        client.post(
            "/api/health/health-connect/sync",
            json={"records": [{"id": "a", "count": 100, "startTime": "2026-08-13T08:00:00Z"}]},
            headers={"Authorization": "Bearer t"},
        )
        _auth_as(monkeypatch, user_id=2)
        client.post(
            "/api/health/health-connect/sync",
            json={"records": [{"id": "b", "count": 200, "startTime": "2026-08-13T08:00:00Z"}]},
            headers={"Authorization": "Bearer t"},
        )
        rows = fake_supabase.tables["health_activity_records"]
        assert len(rows) == 2
        user_ids = {row["user_id"] for row in rows}
        assert user_ids == {1, 2}

    def test_unparseable_record_is_skipped_not_crashed(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": [{"id": "bad", "count": 1, "startTime": ""}]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"received": 1, "stored": 0, "skipped": 1, "debug_last_error": None}

    def test_denied_without_entitlement(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        monkeypatch.setattr(router_module, "has_feature", lambda email, feature: False)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": []},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 403

    def test_requires_auth(self, client):
        resp = client.post("/api/health/health-connect/sync", json={"records": []})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# resolve_trend_source — 3-tier precedence (Google Health > Health Connect > manual)
# ---------------------------------------------------------------------------


class TestResolveTrendSourceHealthConnectTier:
    TODAY = date(2026, 8, 13)

    def _steps_row(self, days_ago: int, value: float) -> dict[str, object]:
        d = self.TODAY - timedelta(days=days_ago)
        return {"start_time": f"{d.isoformat()}T08:00:00Z", "value": value}

    def test_falls_back_to_health_connect_when_google_health_empty(self):
        resolved = ghs.resolve_trend_source(
            signal="steps",
            google_rows=[],
            manual_entries=[],
            today=self.TODAY,
            health_connect_rows=[self._steps_row(1, 4000)],
        )
        assert resolved.source == ghs.SOURCE_HEALTH_CONNECT
        assert resolved.trend.data_points == 1

    def test_google_health_still_wins_when_both_present(self):
        resolved = ghs.resolve_trend_source(
            signal="steps",
            google_rows=[self._steps_row(1, 8000)],
            manual_entries=[],
            today=self.TODAY,
            health_connect_rows=[self._steps_row(1, 4000)],
        )
        assert resolved.source == ghs.SOURCE_GOOGLE_HEALTH
        # Google Health value must NOT be blended/summed with Health Connect's.
        assert resolved.trend.average == 8000

    def test_manual_checkin_is_the_final_fallback(self):
        resolved = ghs.resolve_trend_source(
            signal="steps",
            google_rows=[],
            manual_entries=[{"entry_date": (self.TODAY - timedelta(days=1)).isoformat(), "steps": 3000}],
            today=self.TODAY,
            health_connect_rows=[],
        )
        assert resolved.source == ghs.SOURCE_MANUAL_CHECKIN

    def test_existing_2tier_behavior_unchanged_when_param_omitted(self):
        """The pre-Phase-2 callers never pass health_connect_rows at all —
        must behave byte-identical to before."""
        resolved = ghs.resolve_trend_source(
            signal="steps",
            google_rows=[],
            manual_entries=[{"entry_date": (self.TODAY - timedelta(days=1)).isoformat(), "steps": 3000}],
            today=self.TODAY,
        )
        assert resolved.source == ghs.SOURCE_MANUAL_CHECKIN
