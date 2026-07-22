"""Ownership (Nutzertrennung) tests for `app.routers.twin_memory`.

Mocks the Supabase client so no real network/database access is needed —
verifies that `_require_own_memory`/`_require_own_pattern` raise 404 (never
403, see `core/auth.py`) whenever a row doesn't exist or doesn't belong to
the requesting user's email, exactly like the existing pattern established
for `_require_own_recommendation` (Etappe 4) and `assert_owns` (Etappe 2,
see `tests/test_auth.py`).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers import twin_memory as twin_memory_router


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def neq(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return _FakeResponse(self._data)


class _FakeSupabase:
    def __init__(self, data):
        self._data = data

    def table(self, name):
        return _FakeQuery(self._data)


class TestRequireOwnMemory:
    def test_missing_memory_raises_404(self, monkeypatch):
        monkeypatch.setattr(twin_memory_router, "supabase", _FakeSupabase([]))
        with pytest.raises(HTTPException) as exc_info:
            twin_memory_router._require_own_memory("user-a@example.com", "does-not-exist")
        assert exc_info.value.status_code == 404

    def test_own_memory_is_returned(self, monkeypatch):
        row = {"id": "m1", "email": "user-a@example.com", "status": "candidate"}
        monkeypatch.setattr(twin_memory_router, "supabase", _FakeSupabase([row]))
        result = twin_memory_router._require_own_memory("user-a@example.com", "m1")
        assert result == row

    def test_foreign_memory_is_not_distinguishable_from_missing(self, monkeypatch):
        # The fake query doesn't actually filter by email (that's the real
        # Supabase client's job) — this test asserts the *contract*: an
        # empty result (as the real `.eq("email", email)` filter would
        # produce for another user's row) must yield 404, not a different
        # status code that would let an attacker distinguish "exists but
        # isn't yours" from "doesn't exist".
        monkeypatch.setattr(twin_memory_router, "supabase", _FakeSupabase([]))
        with pytest.raises(HTTPException) as exc_info:
            twin_memory_router._require_own_memory("user-b@example.com", "someone-elses-memory")
        assert exc_info.value.status_code == 404


class TestRequireOwnPattern:
    def test_missing_pattern_raises_404(self, monkeypatch):
        monkeypatch.setattr(twin_memory_router, "supabase", _FakeSupabase([]))
        with pytest.raises(HTTPException) as exc_info:
            twin_memory_router._require_own_pattern("user-a@example.com", "does-not-exist")
        assert exc_info.value.status_code == 404

    def test_own_pattern_is_returned(self, monkeypatch):
        row = {"id": "p1", "email": "user-a@example.com", "status": "active"}
        monkeypatch.setattr(twin_memory_router, "supabase", _FakeSupabase([row]))
        result = twin_memory_router._require_own_pattern("user-a@example.com", "p1")
        assert result == row
