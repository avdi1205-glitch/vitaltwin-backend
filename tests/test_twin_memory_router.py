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


class _TimelineQuery:
    """Table-aware, email-filtering, `.range()`/`.in_()`-capable fake —
    needed for the learning-timeline endpoint's real pagination + isolation
    behavior (unlike `_FakeQuery` above, which ignores `.eq()` filters)."""

    def __init__(self, rows: list[dict]):
        self._all_rows = rows
        self._filters: dict[str, object] = {}
        self._in_filters: dict[str, list[str]] = {}
        self._range: tuple[int, int] | None = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    def in_(self, field, values):
        self._in_filters[field] = [str(v) for v in values]
        return self

    def order(self, *args, **kwargs):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        rows = [r for r in self._all_rows if all(r.get(k) == v for k, v in self._filters.items())]
        for field, values in self._in_filters.items():
            rows = [r for r in rows if str(r.get(field)) in values]
        rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)
        if self._range is not None:
            start, end = self._range
            rows = rows[start : end + 1]
        return _FakeResponse(rows)


class _TimelineSupabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def table(self, name):
        return _TimelineQuery(self._tables.get(name, []))


def _event_row(event_type, source_type, *, email, created_at, source_id="mem-1", **kwargs):
    row = {
        "id": kwargs.pop("id", f"evt-{created_at}"),
        "email": email,
        "event_type": event_type,
        "source_type": source_type,
        "source_id": source_id,
        "created_at": created_at,
        "previous_state": kwargs.pop("previous_state", None),
        "new_state": kwargs.pop("new_state", {}),
        "reason": kwargs.pop("reason", None),
    }
    row.update(kwargs)
    return row


class TestLearningTimelineEndpoint:
    @pytest.mark.anyio
    async def test_empty_timeline_for_new_user(self, monkeypatch):
        fake = _TimelineSupabase({twin_memory_router.LEARNING_EVENT_TABLE: []})
        monkeypatch.setattr(twin_memory_router, "supabase", fake)
        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "new-user@example.com")

        result = await twin_memory_router.get_learning_timeline(authorization="Bearer x")
        assert result["items"] == []
        assert result["has_more"] is False

    @pytest.mark.anyio
    async def test_newest_first_and_customer_safe_mapping(self, monkeypatch):
        rows = [
            _event_row("memory_bestaetigt", "twin_memory", email="user-a@example.com", created_at="2026-08-01T09:00:00+00:00", id="a"),
            _event_row("memory_bestaetigt", "twin_memory", email="user-a@example.com", created_at="2026-08-03T09:00:00+00:00", id="b"),
        ]
        fake = _TimelineSupabase({twin_memory_router.LEARNING_EVENT_TABLE: rows})
        monkeypatch.setattr(twin_memory_router, "supabase", fake)
        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "user-a@example.com")

        result = await twin_memory_router.get_learning_timeline(authorization="Bearer x")
        assert [item["id"] for item in result["items"]] == ["b", "a"]
        assert all("event_type" not in item for item in result["items"])
        assert result["items"][0]["category"] == "CONFIRMED"

    @pytest.mark.anyio
    async def test_user_a_never_sees_user_b_events(self, monkeypatch):
        rows = [
            _event_row("memory_bestaetigt", "twin_memory", email="user-a@example.com", created_at="2026-08-01T09:00:00+00:00", id="a"),
            _event_row("memory_bestaetigt", "twin_memory", email="user-b@example.com", created_at="2026-08-02T09:00:00+00:00", id="b"),
        ]
        fake = _TimelineSupabase({twin_memory_router.LEARNING_EVENT_TABLE: rows})
        monkeypatch.setattr(twin_memory_router, "supabase", fake)
        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "user-a@example.com")

        result = await twin_memory_router.get_learning_timeline(authorization="Bearer x")
        assert [item["id"] for item in result["items"]] == ["a"]

    @pytest.mark.anyio
    async def test_family_membership_grants_no_access_to_another_members_timeline(self, monkeypatch):
        rows = [
            _event_row("memory_bestaetigt", "twin_memory", email="family-owner@example.com", created_at="2026-08-01T09:00:00+00:00", id="owner-evt"),
            _event_row("memory_bestaetigt", "twin_memory", email="family-member@example.com", created_at="2026-08-01T09:00:00+00:00", id="member-evt"),
        ]
        fake = _TimelineSupabase({twin_memory_router.LEARNING_EVENT_TABLE: rows})
        monkeypatch.setattr(twin_memory_router, "supabase", fake)

        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "family-member@example.com")
        member_result = await twin_memory_router.get_learning_timeline(authorization="Bearer x")
        assert [item["id"] for item in member_result["items"]] == ["member-evt"]

        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "family-owner@example.com")
        owner_result = await twin_memory_router.get_learning_timeline(authorization="Bearer x")
        assert [item["id"] for item in owner_result["items"]] == ["owner-evt"]

    @pytest.mark.anyio
    async def test_pagination_limit_and_offset(self, monkeypatch):
        rows = [
            _event_row("memory_bestaetigt", "twin_memory", email="user-a@example.com", created_at=f"2026-08-{d:02d}T09:00:00+00:00", id=f"e{d}")
            for d in range(1, 6)
        ]
        fake = _TimelineSupabase({twin_memory_router.LEARNING_EVENT_TABLE: rows})
        monkeypatch.setattr(twin_memory_router, "supabase", fake)
        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "user-a@example.com")

        page1 = await twin_memory_router.get_learning_timeline(authorization="Bearer x", limit=2, offset=0)
        assert [item["id"] for item in page1["items"]] == ["e5", "e4"]
        assert page1["has_more"] is True

        page2 = await twin_memory_router.get_learning_timeline(authorization="Bearer x", limit=2, offset=2)
        assert [item["id"] for item in page2["items"]] == ["e3", "e2"]

    @pytest.mark.anyio
    async def test_current_state_enrichment_is_scoped_to_the_same_user(self, monkeypatch):
        rows = [_event_row("memory_bestaetigt", "twin_memory", email="user-a@example.com", created_at="2026-08-01T09:00:00+00:00", id="a", source_id="mem-1")]
        memories = [{"id": "mem-1", "email": "user-a@example.com", "status": "confirmed"}]
        fake = _TimelineSupabase({twin_memory_router.LEARNING_EVENT_TABLE: rows, twin_memory_router.MEMORY_TABLE: memories})
        monkeypatch.setattr(twin_memory_router, "supabase", fake)
        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "user-a@example.com")

        result = await twin_memory_router.get_learning_timeline(authorization="Bearer x")
        assert result["items"][0]["current_status"] == "confirmed"
        assert result["items"][0]["is_current"] is True

    @pytest.mark.anyio
    async def test_response_shape_never_exposes_raw_event_type_or_source_ids(self, monkeypatch):
        rows = [_event_row("memory_korrigiert", "twin_memory", email="user-a@example.com", created_at="2026-08-01T09:00:00+00:00", id="a")]
        fake = _TimelineSupabase({twin_memory_router.LEARNING_EVENT_TABLE: rows})
        monkeypatch.setattr(twin_memory_router, "supabase", fake)
        monkeypatch.setattr(twin_memory_router, "_require_email", lambda auth: "user-a@example.com")

        result = await twin_memory_router.get_learning_timeline(authorization="Bearer x")
        item = result["items"][0]
        assert set(item.keys()) == {
            "id", "occurred_at", "category", "related_domain", "title", "summary",
            "confidence_before", "confidence_after", "current_status", "is_current",
        }
