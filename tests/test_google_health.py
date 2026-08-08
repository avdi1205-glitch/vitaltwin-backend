"""Tests for the production-grade Google Health integration:
`core.health_encryption_service`, `core.health_oauth_service`,
`core.google_health_client`, `core.health_connections_repository`,
`core.health_token_service`, `core.health_normalization_service`,
`core.health_sync_service`, and `routers.google_health`.

Mocks Supabase (an in-memory fake supporting the subset of the PostgREST
query builder these modules actually use) and uses `httpx.MockTransport` for
every Google API call — no real network access, no real tokens.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.core import health_connections_repository as repo
from app.core import health_oauth_service as oauth
from app.core import health_sync_service as sync_service
from app.core import health_token_service as token_service
from app.core.google_health_client import GoogleHealthClient
from app.core.health_encryption_service import EncryptionNotConfiguredError, decrypt_secret, encrypt_secret
from app.core.health_errors import HealthIntegrationError
from app.core.health_normalization_service import has_required_scope, normalize_data_point
from app.routers import google_health as router_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_HEALTH_REDIRECT_URI", "https://api.vitaltwin.de/api/health/google/callback")
    monkeypatch.setenv("HEALTH_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-that-is-at-least-32-bytes-long")
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://www.vitaltwin.de")


# ---------------------------------------------------------------------------
# In-memory Supabase fake
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: list[dict], id_counter: itertools.count):
        self._rows = rows
        self._id_counter = id_counter
        self._eq: list[tuple] = []
        self._in: list[tuple] = []
        self._or: str | None = None
        self._order_col: str | None = None
        self._desc = False
        self._limit: int | None = None
        self._op: str | None = None
        self._payload: dict | None = None
        self._on_conflict: str | None = None

    def select(self, *_a, **_k):
        self._op = self._op or "select"
        return self

    def eq(self, field, value):
        self._eq.append((field, value))
        return self

    def in_(self, field, values):
        self._in.append((field, list(values)))
        return self

    def or_(self, expr: str):
        self._or = expr
        return self

    def order(self, field, desc=False):
        self._order_col = field
        self._desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, payload: dict):
        self._op = "insert"
        self._payload = dict(payload)
        return self

    def update(self, payload: dict):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def upsert(self, payload: dict, on_conflict: str | None = None):
        self._op = "upsert"
        self._payload = dict(payload)
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def _matches(self, row: dict) -> bool:
        for field, value in self._eq:
            if row.get(field) != value:
                return False
        for field, values in self._in:
            if row.get(field) not in values:
                return False
        if self._or:
            if not self._eval_or(row, self._or):
                return False
        return True

    @staticmethod
    def _eval_or(row: dict, expr: str) -> bool:
        for clause in expr.split(","):
            field, op, value = clause.split(".", 2)
            actual = row.get(field)
            if op == "is" and value == "null" and actual is None:
                return True
            if op == "lt" and actual is not None and str(actual) < value:
                return True
        return False

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

        if self._op == "update":
            matched = [r for r in self._rows if self._matches(r)]
            for row in matched:
                row.update(self._payload or {})
            return SimpleNamespace(data=matched)

        if self._op == "delete":
            matched = [r for r in self._rows if self._matches(r)]
            for row in matched:
                self._rows.remove(row)
            return SimpleNamespace(data=matched)

        # select
        matched = [r for r in self._rows if self._matches(r)]
        if self._order_col:
            matched = sorted(matched, key=lambda r: (r.get(self._order_col) is None, r.get(self._order_col)), reverse=self._desc)
        if self._limit is not None:
            matched = matched[: self._limit]
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
    for module in (repo, oauth, sync_service, router_module):
        monkeypatch.setattr(module, "supabase", fake, raising=False)
    return fake


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class TestEncryption:
    def test_round_trip(self):
        encrypted = encrypt_secret("super-secret-token")
        assert decrypt_secret(encrypted) == "super-secret-token"

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("HEALTH_TOKEN_ENCRYPTION_KEY", raising=False)
        with pytest.raises(EncryptionNotConfiguredError):
            encrypt_secret("token")


# ---------------------------------------------------------------------------
# OAuth state (DB-backed, single-use)
# ---------------------------------------------------------------------------


class TestOAuthState:
    def test_round_trip(self, fake_supabase):
        state = oauth.create_oauth_state(user_id=42, requested_scopes=oauth.DEFAULT_SCOPES)
        row = oauth.consume_oauth_state(state)
        assert row["user_id"] == 42

    def test_cannot_be_used_twice(self, fake_supabase):
        state = oauth.create_oauth_state(user_id=1, requested_scopes=oauth.DEFAULT_SCOPES)
        oauth.consume_oauth_state(state)
        with pytest.raises(HealthIntegrationError) as exc_info:
            oauth.consume_oauth_state(state)
        assert exc_info.value.code == "HEALTH_OAUTH_STATE_USED"

    def test_unknown_state_rejected(self, fake_supabase):
        with pytest.raises(HealthIntegrationError) as exc_info:
            oauth.consume_oauth_state("never-issued")
        assert exc_info.value.code == "HEALTH_OAUTH_STATE_INVALID"

    def test_expired_state_rejected(self, fake_supabase, monkeypatch):
        monkeypatch.setenv("HEALTH_OAUTH_STATE_TTL_SECONDS", "0")
        state = oauth.create_oauth_state(user_id=1, requested_scopes=oauth.DEFAULT_SCOPES)
        # Force expiry into the past regardless of clock resolution.
        fake_supabase.tables["health_oauth_states"][0]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()
        with pytest.raises(HealthIntegrationError) as exc_info:
            oauth.consume_oauth_state(state)
        assert exc_info.value.code == "HEALTH_OAUTH_STATE_EXPIRED"


class TestAuthorizationUrl:
    def test_includes_required_params(self):
        url = oauth.build_authorization_url(state="abc")
        assert url.startswith(oauth.authorization_endpoint())
        assert "client_id=test-client-id" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "include_granted_scopes=true" in url
        assert "state=abc" in url

    def test_missing_config_raises(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
        with pytest.raises(HealthIntegrationError) as exc_info:
            oauth.build_authorization_url(state="abc")
        assert exc_info.value.code == "HEALTH_NOT_CONFIGURED"


class TestTokenExchange:
    @pytest.mark.anyio
    async def test_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "s1"})

        result = await oauth.exchange_code_for_tokens("code123", transport=httpx.MockTransport(handler))
        assert result["access_token"] == "at"

    @pytest.mark.anyio
    async def test_failure_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        with pytest.raises(HealthIntegrationError) as exc_info:
            await oauth.exchange_code_for_tokens("bad", transport=httpx.MockTransport(handler))
        assert exc_info.value.code == "HEALTH_TOKEN_EXCHANGE_FAILED"


# ---------------------------------------------------------------------------
# Google Health API client
# ---------------------------------------------------------------------------


class TestGoogleHealthClient:
    @pytest.mark.anyio
    async def test_get_identity_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"healthUserId": "abc", "legacyUserId": "xyz"})

        client = GoogleHealthClient(access_token="at", transport=httpx.MockTransport(handler))
        identity = await client.get_identity()
        assert identity["healthUserId"] == "abc"

    @pytest.mark.anyio
    async def test_401_maps_to_reauth_required(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_token"})

        client = GoogleHealthClient(access_token="expired", transport=httpx.MockTransport(handler))
        with pytest.raises(HealthIntegrationError) as exc_info:
            await client.get_identity()
        assert exc_info.value.code == "HEALTH_REAUTH_REQUIRED"

    @pytest.mark.anyio
    async def test_429_maps_to_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "rate_limited"})

        client = GoogleHealthClient(access_token="at", transport=httpx.MockTransport(handler))
        with pytest.raises(HealthIntegrationError) as exc_info:
            await client.list_data_points_page(data_type="steps")
        assert exc_info.value.code == "HEALTH_RATE_LIMITED"

    @pytest.mark.anyio
    async def test_500_maps_to_provider_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = GoogleHealthClient(access_token="at", transport=httpx.MockTransport(handler))
        with pytest.raises(HealthIntegrationError) as exc_info:
            await client.list_data_points_page(data_type="steps")
        assert exc_info.value.code == "HEALTH_PROVIDER_UNAVAILABLE"

    @pytest.mark.anyio
    async def test_iter_data_points_paginates(self):
        pages = [
            {"dataPoints": [{"name": "dp1"}], "nextPageToken": "page2"},
            {"dataPoints": [{"name": "dp2"}]},
        ]
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            response = pages[call_count["n"]]
            call_count["n"] += 1
            return httpx.Response(200, json=response)

        client = GoogleHealthClient(access_token="at", transport=httpx.MockTransport(handler))
        items = [item async for item in client.iter_data_points(data_type="steps")]
        assert [i["name"] for i in items] == ["dp1", "dp2"]

    @pytest.mark.anyio
    async def test_iter_data_points_respects_max_pages_safety_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            # Always returns a next page token -> would loop forever without the cap.
            return httpx.Response(200, json={"dataPoints": [{"name": "dp"}], "nextPageToken": "again"})

        client = GoogleHealthClient(access_token="at", transport=httpx.MockTransport(handler))
        items = [item async for item in client.iter_data_points(data_type="steps", max_pages=3)]
        assert len(items) == 3


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_has_required_scope(self):
        assert has_required_scope("steps", [
            "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
        ])
        assert not has_required_scope("steps", ["https://www.googleapis.com/auth/googlehealth.sleep.readonly"])

    def test_unknown_data_type_has_no_scope(self):
        assert not has_required_scope("unknown-type", [])

    def test_normalize_activity_point(self):
        row = normalize_data_point(
            "steps",
            {
                "name": "dp1",
                "interval": {"startTime": "2026-01-01T00:00:00+00:00", "endTime": "2026-01-01T00:05:00+00:00"},
                "value": {"intValue": 120},
            },
        )
        assert row is not None
        assert row["data_type"] == "steps"
        assert row["value"] == 120
        assert row["unit"] == "count"

    def test_normalize_sleep_point(self):
        row = normalize_data_point(
            "sleep",
            {
                "name": "dp2",
                "session": {"startTime": "2026-01-01T22:00:00+00:00", "endTime": "2026-01-02T06:00:00+00:00"},
                "sleepStage": "deep",
            },
        )
        assert row is not None
        assert row["sleep_stage"] == "deep"

    def test_normalize_metric_point(self):
        row = normalize_data_point(
            "heart-rate",
            {"name": "dp3", "sampleTime": {"physicalTime": "2026-01-01T08:00:00+00:00"}, "value": {"floatValue": 62.0}},
        )
        assert row is not None
        assert row["observed_at"] == "2026-01-01T08:00:00+00:00"
        assert row["value"] == 62.0

    def test_unknown_data_type_returns_none(self):
        assert normalize_data_point("unknown-type", {"name": "dp"}) is None

    def test_missing_timestamp_returns_none(self):
        assert normalize_data_point("steps", {"name": "dp1", "value": {"intValue": 1}}) is None


# ---------------------------------------------------------------------------
# Connections repository
# ---------------------------------------------------------------------------


class TestConnectionsRepository:
    def test_upsert_creates_then_reuses_row(self, fake_supabase):
        first = repo.upsert_connection(
            user_id=1,
            encrypted_access_token="a",
            encrypted_refresh_token="b",
            access_token_expires_at="2026-01-01T00:00:00+00:00",
            granted_scopes=["s1"],
            provider_health_user_id="h1",
            provider_legacy_user_id=None,
        )
        second = repo.upsert_connection(
            user_id=1,
            encrypted_access_token="c",
            encrypted_refresh_token="d",
            access_token_expires_at="2026-02-01T00:00:00+00:00",
            granted_scopes=["s1", "s2"],
            provider_health_user_id="h1",
            provider_legacy_user_id=None,
        )
        assert first["id"] == second["id"]
        assert len(fake_supabase.tables["user_health_connections"]) == 1
        assert second["encrypted_access_token"] == "c"

    def test_get_active_connection_excludes_disconnected(self, fake_supabase):
        connection = repo.upsert_connection(
            user_id=2,
            encrypted_access_token="a",
            encrypted_refresh_token="b",
            access_token_expires_at="2026-01-01T00:00:00+00:00",
            granted_scopes=[],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )
        repo.disconnect_connection(connection["id"])
        assert repo.get_active_connection(2) is None
        assert repo.get_any_connection(2)["status"] == "disconnected"

    def test_refresh_lock_acquire_and_release(self, fake_supabase):
        connection = repo.upsert_connection(
            user_id=3,
            encrypted_access_token="a",
            encrypted_refresh_token="b",
            access_token_expires_at="2026-01-01T00:00:00+00:00",
            granted_scopes=[],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )
        token = repo.try_acquire_refresh_lock(connection["id"])
        assert token is not None
        # Second attempt should fail while the lock is held.
        assert repo.try_acquire_refresh_lock(connection["id"]) is None
        repo.release_refresh_lock(connection["id"], token)
        assert repo.try_acquire_refresh_lock(connection["id"]) is not None


# ---------------------------------------------------------------------------
# Token service
# ---------------------------------------------------------------------------


class TestTokenService:
    @pytest.mark.anyio
    async def test_returns_existing_token_when_not_expired(self, fake_supabase):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        connection = repo.upsert_connection(
            user_id=1,
            encrypted_access_token=encrypt_secret("at"),
            encrypted_refresh_token=encrypt_secret("rt"),
            access_token_expires_at=future,
            granted_scopes=[],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )
        access_token, _ = await token_service.get_valid_access_token(connection)
        assert access_token == "at"

    @pytest.mark.anyio
    async def test_refreshes_expired_token(self, fake_supabase, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        connection = repo.upsert_connection(
            user_id=1,
            encrypted_access_token=encrypt_secret("old-at"),
            encrypted_refresh_token=encrypt_secret("rt"),
            access_token_expires_at=past,
            granted_scopes=[],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )

        async def fake_refresh(refresh_token, transport=None):
            assert refresh_token == "rt"
            return {"access_token": "new-at", "expires_in": 3600}

        monkeypatch.setattr(token_service, "refresh_access_token", fake_refresh)
        access_token, refreshed = await token_service.get_valid_access_token(connection)
        assert access_token == "new-at"
        assert decrypt_secret(refreshed["encrypted_access_token"]) == "new-at"

    @pytest.mark.anyio
    async def test_refresh_failure_marks_reauthorization_required(self, fake_supabase, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        connection = repo.upsert_connection(
            user_id=1,
            encrypted_access_token=encrypt_secret("old-at"),
            encrypted_refresh_token=encrypt_secret("rt"),
            access_token_expires_at=past,
            granted_scopes=[],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )

        async def fake_refresh(refresh_token, transport=None):
            raise HealthIntegrationError("HEALTH_REFRESH_FAILED", "invalid_grant")

        monkeypatch.setattr(token_service, "refresh_access_token", fake_refresh)
        with pytest.raises(HealthIntegrationError) as exc_info:
            await token_service.get_valid_access_token(connection)
        assert exc_info.value.code == "HEALTH_REAUTH_REQUIRED"
        assert repo.get_connection_by_id(connection["id"])["status"] == "reauthorization_required"


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------


class TestSyncUserHealthData:
    @pytest.mark.anyio
    async def test_skips_data_type_missing_scope(self, fake_supabase, monkeypatch):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        connection = repo.upsert_connection(
            user_id=1,
            encrypted_access_token=encrypt_secret("at"),
            encrypted_refresh_token=encrypt_secret("rt"),
            access_token_expires_at=future,
            granted_scopes=["https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )

        async def fake_iter(self, *, data_type, start_time=None, end_time=None, page_size=None, max_pages=None):
            return
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(GoogleHealthClient, "iter_data_points", fake_iter)
        result = await sync_service.sync_user_health_data(
            user_id=1, connection=connection, requested_data_types=["steps", "sleep"]
        )
        assert result["per_type"]["sleep"]["error_code"] == "HEALTH_SCOPE_MISSING"
        assert result["per_type"]["steps"]["error_code"] is None

    @pytest.mark.anyio
    async def test_full_success_updates_counters_and_connection(self, fake_supabase, monkeypatch):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        connection = repo.upsert_connection(
            user_id=1,
            encrypted_access_token=encrypt_secret("at"),
            encrypted_refresh_token=encrypt_secret("rt"),
            access_token_expires_at=future,
            granted_scopes=["https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )

        async def fake_iter(self, *, data_type, start_time=None, end_time=None, page_size=None, max_pages=None):
            yield {
                "name": "dp1",
                "interval": {"startTime": "2026-01-01T00:00:00+00:00", "endTime": "2026-01-01T00:05:00+00:00"},
                "value": {"intValue": 500},
            }

        monkeypatch.setattr(GoogleHealthClient, "iter_data_points", fake_iter)
        result = await sync_service.sync_user_health_data(
            user_id=1, connection=connection, requested_data_types=["steps"]
        )
        assert result["status"] == "completed"
        assert result["counters"]["received"] == 1
        assert result["counters"]["created"] == 1
        assert len(fake_supabase.tables["health_activity_records"]) == 1
        assert repo.get_connection_by_id(connection["id"])["last_sync_status"] == "completed"


# ---------------------------------------------------------------------------
# Router endpoints
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self):
        self.client = SimpleNamespace(host="127.0.0.1")


@pytest.fixture
def current_user(monkeypatch):
    from app.core.auth import CurrentUser

    user = CurrentUser(email="user@example.com", user_id=7)
    monkeypatch.setattr(router_module, "require_user", lambda authorization: user)
    monkeypatch.setattr(router_module, "enforce_rate_limit", lambda *a, **k: None)
    return user


class TestProvidersAndConnectionsEndpoints:
    @pytest.mark.anyio
    async def test_providers_not_connected(self, fake_supabase, current_user):
        result = await router_module.list_health_providers(authorization="Bearer x")
        assert result["providers"][0]["status"] == "not_connected"

    @pytest.mark.anyio
    async def test_connections_empty_list(self, fake_supabase, current_user):
        result = await router_module.list_health_connections(authorization="Bearer x")
        assert result["connections"] == []


class TestConnectEndpoint:
    @pytest.mark.anyio
    async def test_returns_authorization_url(self, fake_supabase, current_user):
        result = await router_module.start_google_health_connect(_FakeRequest(), authorization="Bearer x")
        assert "authorization_url" in result
        assert "include_granted_scopes=true" in result["authorization_url"]


class TestCallbackEndpoint:
    @pytest.mark.anyio
    async def test_missing_code_or_state_redirects_with_error(self, fake_supabase):
        response = await router_module.google_health_callback(code=None, state=None)
        assert response.status_code == 307
        assert "HEALTH_OAUTH_STATE_INVALID" in response.headers["location"]

    @pytest.mark.anyio
    async def test_provider_error_param_redirects(self, fake_supabase):
        response = await router_module.google_health_callback(code=None, state=None, error="access_denied")
        assert "HEALTH_OAUTH_DENIED" in response.headers["location"]

    @pytest.mark.anyio
    async def test_invalid_state_redirects_with_reason(self, fake_supabase):
        response = await router_module.google_health_callback(code="abc", state="never-issued")
        assert "HEALTH_OAUTH_STATE_INVALID" in response.headers["location"]

    @pytest.mark.anyio
    async def test_successful_exchange_stores_connection_and_redirects(self, fake_supabase, monkeypatch):
        state = oauth.create_oauth_state(user_id=7, requested_scopes=oauth.DEFAULT_SCOPES)

        async def fake_exchange(code, transport=None):
            return {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": " ".join(oauth.DEFAULT_SCOPES),
            }

        async def fake_get_identity(self):
            return {"healthUserId": "hid", "legacyUserId": "lid"}

        monkeypatch.setattr(router_module.oauth, "exchange_code_for_tokens", fake_exchange)
        monkeypatch.setattr(GoogleHealthClient, "get_identity", fake_get_identity)

        response = await router_module.google_health_callback(code="abc", state=state)
        assert "health_connect=success" in response.headers["location"]
        stored = repo.get_active_connection(7)
        assert stored is not None
        assert stored["provider_health_user_id"] == "hid"

    @pytest.mark.anyio
    async def test_missing_refresh_token_redirects_with_error(self, fake_supabase, monkeypatch):
        state = oauth.create_oauth_state(user_id=7, requested_scopes=oauth.DEFAULT_SCOPES)

        async def fake_exchange(code, transport=None):
            return {"access_token": "at", "expires_in": 3600, "scope": "s1"}

        monkeypatch.setattr(router_module.oauth, "exchange_code_for_tokens", fake_exchange)
        response = await router_module.google_health_callback(code="abc", state=state)
        assert "HEALTH_NO_REFRESH_TOKEN" in response.headers["location"]


class TestStatusAndDisconnectEndpoints:
    @pytest.mark.anyio
    async def test_status_not_connected(self, fake_supabase, current_user):
        result = await router_module.google_health_status(authorization="Bearer x")
        assert result["connected"] is False

    @pytest.mark.anyio
    async def test_disconnect_without_connection_raises_404(self, fake_supabase, current_user):
        with pytest.raises(HTTPException) as exc_info:
            await router_module.google_health_disconnect(authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_disconnect_clears_tokens(self, fake_supabase, current_user, monkeypatch):
        repo.upsert_connection(
            user_id=7,
            encrypted_access_token=encrypt_secret("at"),
            encrypted_refresh_token=encrypt_secret("rt"),
            access_token_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            granted_scopes=[],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )

        async def fake_revoke(token, transport=None):
            return None

        monkeypatch.setattr(router_module.oauth, "revoke_token", fake_revoke)
        result = await router_module.google_health_disconnect(authorization="Bearer x")
        assert result["disconnected"] is True
        connection = repo.get_any_connection(7)
        assert connection["status"] == "disconnected"
        assert connection["encrypted_access_token"] == ""


class TestSyncEndpoint:
    @pytest.mark.anyio
    async def test_sync_without_connection_raises_404(self, fake_supabase, current_user):
        with pytest.raises(HTTPException) as exc_info:
            await router_module.google_health_sync(_FakeRequest(), authorization="Bearer x")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_sync_requiring_reauth_raises_409(self, fake_supabase, current_user):
        connection = repo.upsert_connection(
            user_id=7,
            encrypted_access_token=encrypt_secret("at"),
            encrypted_refresh_token=encrypt_secret("rt"),
            access_token_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            granted_scopes=[],
            provider_health_user_id=None,
            provider_legacy_user_id=None,
        )
        repo.mark_reauthorization_required(connection["id"], "test")
        with pytest.raises(HTTPException) as exc_info:
            await router_module.google_health_sync(_FakeRequest(), authorization="Bearer x")
        assert exc_info.value.status_code == 409


class TestDataEndpoints:
    @pytest.mark.anyio
    async def test_activity_endpoint_scopes_to_current_user(self, fake_supabase, current_user):
        fake_supabase.tables["health_activity_records"] = [
            {"id": 1, "user_id": 7, "data_type": "steps", "start_time": "2026-01-01T00:00:00+00:00", "value": 100},
            {"id": 2, "user_id": 99, "data_type": "steps", "start_time": "2026-01-01T00:00:00+00:00", "value": 999},
        ]
        result = await router_module.get_activity_data(authorization="Bearer x")
        assert len(result["items"]) == 1
        assert result["items"][0]["user_id"] == 7

    @pytest.mark.anyio
    async def test_activity_endpoint_rejects_invalid_data_type(self, fake_supabase, current_user):
        with pytest.raises(HTTPException) as exc_info:
            await router_module.get_activity_data(data_type="sleep", authorization="Bearer x")
        assert exc_info.value.status_code == 400
