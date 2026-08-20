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

    def insert(self, payload: dict):
        self._op = "insert"
        self._payload = dict(payload)
        return self

    def _matches(self, row: dict) -> bool:
        return all(row.get(field) == value for field, value in self._eq)

    def execute(self):
        if self._op == "insert":
            row = dict(self._payload or {})
            row.setdefault("id", next(self._id_counter))
            self._rows.append(row)
            return SimpleNamespace(data=[row])
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
            json={"records": {"steps": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z", "endTime": "2026-08-13T09:00:00Z"}]}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"]["steps"] == {"received": 1, "stored": 1, "skipped": 0}
        assert body["unsupported_types"] == []
        assert body["debug_last_error"] is None
        rows = fake_supabase.tables["health_activity_records"]
        assert len(rows) == 1
        assert rows[0]["provider"] == "health_connect"
        assert rows[0]["connection_id"] is None
        assert rows[0]["user_id"] == 42
        assert rows[0]["data_type"] == "steps"
        assert rows[0]["value"] == 500.0

    def test_resyncing_the_same_record_does_not_duplicate(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        payload = {"records": {"steps": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z"}]}}
        client.post("/api/health/health-connect/sync", json=payload, headers={"Authorization": "Bearer t"})
        client.post("/api/health/health-connect/sync", json=payload, headers={"Authorization": "Bearer t"})
        assert len(fake_supabase.tables["health_activity_records"]) == 1

    def test_two_users_do_not_see_each_others_rows(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=1)
        client.post(
            "/api/health/health-connect/sync",
            json={"records": {"steps": [{"id": "a", "count": 100, "startTime": "2026-08-13T08:00:00Z"}]}},
            headers={"Authorization": "Bearer t"},
        )
        _auth_as(monkeypatch, user_id=2)
        client.post(
            "/api/health/health-connect/sync",
            json={"records": {"steps": [{"id": "b", "count": 200, "startTime": "2026-08-13T08:00:00Z"}]}},
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
            json={"records": {"steps": [{"id": "bad", "count": 1, "startTime": ""}]}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert resp.json()["results"]["steps"] == {"received": 1, "stored": 0, "skipped": 1}


# ---------------------------------------------------------------------------
# health_sync_runs logging (Phase 2.3 background sync)
# ---------------------------------------------------------------------------


class TestHealthConnectSyncRunLogging:
    def test_manual_sync_defaults_and_logs_a_completed_run(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {"steps": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z"}]}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        runs = fake_supabase.tables["health_sync_runs"]
        assert len(runs) == 1
        assert runs[0]["provider"] == "health_connect"
        assert runs[0]["connection_id"] is None
        assert runs[0]["sync_type"] == "manual"
        assert runs[0]["status"] == "completed"
        assert runs[0]["records_received"] == 1
        assert runs[0]["records_created"] == 1
        assert runs[0]["records_skipped"] == 0
        assert runs[0]["user_id"] == 42

    def test_background_sync_type_is_accepted_and_logged(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={
                "records": {"steps": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z"}]},
                "sync_type": "background",
            },
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        runs = fake_supabase.tables["health_sync_runs"]
        assert runs[0]["sync_type"] == "background"

    def test_invalid_sync_type_is_rejected(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {}, "sync_type": "scheduled_cron"},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 422

    def test_partial_status_when_some_records_skipped(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={
                "records": {
                    "steps": [
                        {"id": "good", "count": 500, "startTime": "2026-08-13T08:00:00Z"},
                        {"id": "bad", "count": 1, "startTime": ""},
                    ]
                }
            },
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        runs = fake_supabase.tables["health_sync_runs"]
        assert runs[0]["status"] == "partial"
        assert runs[0]["records_created"] == 1
        assert runs[0]["records_skipped"] == 1

    def test_no_run_logged_when_no_records_and_nothing_unsupported(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert fake_supabase.tables.get("health_sync_runs", []) == []

    def test_logging_failure_does_not_break_the_sync_response(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)

        def _boom(name):
            if name == "health_sync_runs":
                raise RuntimeError("db unavailable")
            return _FakeQuery(fake_supabase.tables.setdefault(name, []), fake_supabase._id_counter)

        monkeypatch.setattr(fake_supabase, "table", _boom)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {"steps": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z"}]}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert resp.json()["results"]["steps"]["stored"] == 1

    def test_denied_without_entitlement(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        monkeypatch.setattr(router_module, "has_feature", lambda email, feature: False)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 403

    def test_requires_auth(self, client):
        resp = client.post("/api/health/health-connect/sync", json={"records": {}})
        assert resp.status_code == 401

    def test_unsupported_data_type_is_reported_not_guessed(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {"blood-pressure": [{"id": "x"}]}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["unsupported_types"] == ["blood-pressure"]
        assert body["results"] == {}

    def test_metric_type_stores_into_health_metric_records(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {"resting-heart-rate": [{"id": "rhr-1", "beatsPerMinute": 58, "time": "2026-08-13T05:00:00Z"}]}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert resp.json()["results"]["resting-heart-rate"] == {"received": 1, "stored": 1, "skipped": 0}
        rows = fake_supabase.tables["health_metric_records"]
        assert rows[0]["provider"] == "health_connect"
        assert rows[0]["data_type"] == "resting-heart-rate"
        assert rows[0]["value"] == 58.0
        assert rows[0]["unit"] == "bpm"

    def test_exercise_session_stores_duration_and_metadata(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={
                "records": {
                    "exercise-session": [
                        {
                            "id": "ex-1",
                            "durationSeconds": 1800,
                            "exerciseType": "running",
                            "title": "Morning run",
                            "startTime": "2026-08-13T06:00:00Z",
                            "endTime": "2026-08-13T06:30:00Z",
                        }
                    ]
                }
            },
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        rows = fake_supabase.tables["health_activity_records"]
        assert rows[0]["data_type"] == "exercise-session"
        assert rows[0]["value"] == 1800.0
        assert rows[0]["raw_metadata"]["exerciseType"] == "running"

    def test_sleep_session_with_stages_produces_one_row_per_stage(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={
                "records": {
                    "sleep-session": [
                        {
                            "id": "sleep-1",
                            "startTime": "2026-08-13T22:00:00Z",
                            "endTime": "2026-08-14T06:00:00Z",
                            "stages": [
                                {"stage": "deep", "startTime": "2026-08-13T22:00:00Z", "endTime": "2026-08-13T23:00:00Z"},
                                {"stage": "rem", "startTime": "2026-08-13T23:00:00Z", "endTime": "2026-08-14T00:00:00Z"},
                            ],
                        }
                    ]
                }
            },
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert resp.json()["results"]["sleep-session"] == {"received": 1, "stored": 2, "skipped": 0}
        rows = fake_supabase.tables["health_sleep_records"]
        assert len(rows) == 2
        assert {r["provider_record_name"] for r in rows} == {"sleep-1:stage:0", "sleep-1:stage:1"}
        assert {r["sleep_stage"] for r in rows} == {"deep", "rem"}
        assert rows[0]["duration_seconds"] == 3600

    def test_sleep_session_without_stages_produces_one_summary_row(self, client, fake_supabase, monkeypatch):
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={
                "records": {
                    "sleep-session": [
                        {"id": "sleep-2", "startTime": "2026-08-13T22:00:00Z", "endTime": "2026-08-14T06:00:00Z"}
                    ]
                }
            },
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        rows = fake_supabase.tables["health_sleep_records"]
        assert len(rows) == 1
        assert rows[0]["sleep_stage"] is None
        assert rows[0]["provider_record_name"] == "sleep-2"
        assert rows[0]["duration_seconds"] == 8 * 3600

    def test_permission_denied_category_is_simply_absent_others_still_work(self, client, fake_supabase, monkeypatch):
        """No special-casing needed: a category the user never granted is
        simply never included in the payload — the other categories must
        still sync normally."""
        _auth_as(monkeypatch, user_id=42)
        resp = client.post(
            "/api/health/health-connect/sync",
            json={"records": {"steps": [{"id": "r1", "count": 500, "startTime": "2026-08-13T08:00:00Z"}]}},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        assert "weight" not in resp.json()["results"]
        assert resp.json()["results"]["steps"]["stored"] == 1


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


# ---------------------------------------------------------------------------
# resolve_two_tier_source — Google Health > Health Connect, NO manual
# fallback (signals with no manual check-in counterpart at all).
# ---------------------------------------------------------------------------


class TestResolveTwoTierSource:
    TODAY = date(2026, 8, 13)

    def _row(self, days_ago: int, value: float) -> dict[str, object]:
        d = self.TODAY - timedelta(days=days_ago)
        return {"observed_at": f"{d.isoformat()}T08:00:00Z", "value": value}

    def test_google_health_wins_when_present(self):
        resolved = ghs.resolve_two_tier_source(
            signal="weight",
            google_rows=[self._row(1, 82.0)],
            today=self.TODAY,
            health_connect_rows=[self._row(1, 80.0)],
        )
        assert resolved.source == ghs.SOURCE_GOOGLE_HEALTH
        assert resolved.trend.average == 82.0

    def test_falls_back_to_health_connect_when_google_health_empty(self):
        resolved = ghs.resolve_two_tier_source(
            signal="resting_heart_rate",
            google_rows=[],
            today=self.TODAY,
            health_connect_rows=[self._row(1, 58.0)],
        )
        assert resolved.source == ghs.SOURCE_HEALTH_CONNECT
        assert resolved.trend.data_points == 1

    def test_no_manual_fallback_neither_source_has_data(self):
        resolved = ghs.resolve_two_tier_source(
            signal="oxygen_saturation",
            google_rows=[],
            today=self.TODAY,
            health_connect_rows=[],
        )
        assert resolved.source == ghs.SOURCE_NONE
        assert resolved.trend.data_points == 0


# ---------------------------------------------------------------------------
# normalize_health_connect_record — generic Phase 2.2 normalizer
# ---------------------------------------------------------------------------


class TestNormalizeHealthConnectRecord:
    def test_unknown_data_type_returns_empty_list(self):
        from app.core.health_normalization_service import normalize_health_connect_record

        assert normalize_health_connect_record("blood-pressure", {"id": "x"}) == []

    def test_instant_metric_shape(self):
        from app.core.health_normalization_service import normalize_health_connect_record

        rows = normalize_health_connect_record(
            "heart-rate-variability", {"id": "hrv-1", "rmssdMillis": 42.5, "time": "2026-08-13T05:00:00Z"}
        )
        assert len(rows) == 1
        assert rows[0]["data_type"] == "heart-rate-variability"
        assert rows[0]["value"] == 42.5
        assert rows[0]["unit"] == "ms"
        assert rows[0]["observed_at"] == "2026-08-13T05:00:00Z"
        assert rows[0]["start_time"] is None

    def test_missing_value_key_returns_empty_list(self):
        from app.core.health_normalization_service import normalize_health_connect_record

        assert normalize_health_connect_record("weight", {"id": "w1", "time": "2026-08-13T05:00:00Z"}) == []
