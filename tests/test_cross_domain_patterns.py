"""Integration tests for cross-domain pattern detection wired into
`routers/twin_memory.py::list_patterns` (Twin Core Phase 3). Mocks the
Supabase client — no real network/database access. Follows the same
"call the async route function directly, monkeypatch `_require_email`"
convention used throughout the test suite (e.g. test_trends_router.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routers import twin_memory as twin_memory_module


class _Query:
    def __init__(self, table_name, dataset, calls_log):
        self._table = table_name
        self._dataset = dataset
        self._calls_log = calls_log
        self._filters: dict[str, object] = {}
        self._insert_payload = None
        self._update_payload = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._calls_log.append((self._table, field, value))
        self._filters[field] = value
        return self

    def gte(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def neq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def insert(self, payload):
        self._insert_payload = payload
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def execute(self):
        if self._insert_payload is not None:
            row = {**self._insert_payload, "id": f"new-{self._table}-{len(self._dataset.get(self._table, []))}"}
            self._dataset.setdefault(self._table, []).append(row)
            return SimpleNamespace(data=[row])
        if self._update_payload is not None:
            return SimpleNamespace(data=[self._update_payload])
        rows = self._dataset.get(self._table, [])
        filtered = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
        return SimpleNamespace(data=filtered)


class _FakeSupabase:
    def __init__(self, dataset: dict[str, list[dict]] | None = None):
        self.dataset = dataset or {}
        self.calls: list[tuple[str, str, object]] = []

    def table(self, name):
        return _Query(name, self.dataset, self.calls)


def _google_steps_row(user_id: int, day: str, value: float) -> dict:
    return {"user_id": user_id, "data_type": "steps", "start_time": f"{day}T08:00:00+00:00", "value": value}


def _cgm_row(email: str, day: str, value: float) -> dict:
    return {"email": email, "glucose_value": value, "reading_at": f"{day}T08:00:00+00:00"}


def _real_dataset(*, user_id: int, email: str) -> dict[str, list[dict]]:
    days = [f"2026-08-{d:02d}" for d in range(1, 7)]
    steps = [3000, 4000, 5000, 6000, 7000, 8000]
    glucose = [90, 95, 100, 105, 110, 115]
    return {
        twin_memory_module.HEALTH_ACTIVITY_TABLE: [_google_steps_row(user_id, d, v) for d, v in zip(days, steps)],
        twin_memory_module.CGM_TABLE: [_cgm_row(email, d, v) for d, v in zip(days, glucose)],
    }


@pytest.mark.anyio
class TestCrossDomainSignalLoading:
    async def test_health_tables_scoped_by_user_id_never_email(self, monkeypatch):
        fake = _FakeSupabase(_real_dataset(user_id=1, email="user-a@example.com"))
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda email: 1)

        await twin_memory_module.list_patterns(authorization="Bearer x")

        health_calls = [c for c in fake.calls if c[0] == twin_memory_module.HEALTH_ACTIVITY_TABLE]
        assert any(field == "user_id" for _, field, _ in health_calls)
        assert not any(field == "email" for _, field, _ in health_calls)

    async def test_valid_cross_domain_pattern_is_created_and_persisted(self, monkeypatch):
        fake = _FakeSupabase(_real_dataset(user_id=1, email="user-a@example.com"))
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda email: 1)

        result = await twin_memory_module.list_patterns(authorization="Bearer x")

        pattern_types = {row.get("pattern_type") for row in result["items"]}
        assert "aktivitaet_glukose_gleicher_tag" in pattern_types
        persisted = fake.dataset.get(twin_memory_module.PATTERN_TABLE, [])
        assert any(row.get("pattern_type") == "aktivitaet_glukose_gleicher_tag" for row in persisted)

    async def test_no_causality_wording_in_persisted_pattern(self, monkeypatch):
        fake = _FakeSupabase(_real_dataset(user_id=1, email="user-a@example.com"))
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda email: 1)

        result = await twin_memory_module.list_patterns(authorization="Bearer x")
        cross_domain = next(r for r in result["items"] if r.get("pattern_type") == "aktivitaet_glukose_gleicher_tag")
        assert "verursacht" not in cross_domain["summary"].lower()
        assert "möglicherweise" in cross_domain["summary"]

    async def test_mixed_email_and_user_id_resolve_to_same_authenticated_user(self, monkeypatch):
        """Step 11: mixed email/user_id sources must resolve to the SAME
        authenticated user only — `get_user_id_by_email` is always called
        with the requesting user's OWN email (never a client-supplied id)."""
        seen_emails = []

        def _tracking_get_user_id(email):
            seen_emails.append(email)
            return 1

        fake = _FakeSupabase(_real_dataset(user_id=1, email="user-a@example.com"))
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", _tracking_get_user_id)

        await twin_memory_module.list_patterns(authorization="Bearer x")
        assert seen_emails == ["user-a@example.com"]


@pytest.mark.anyio
class TestCrossDomainIsolation:
    async def test_user_a_health_data_never_enters_user_b_pattern_calculation(self, monkeypatch):
        dataset = {
            twin_memory_module.HEALTH_ACTIVITY_TABLE: (
                _real_dataset(user_id=1, email="user-a@example.com")[twin_memory_module.HEALTH_ACTIVITY_TABLE]
                + [_google_steps_row(2, f"2026-08-{d:02d}", 1) for d in range(1, 7)]
            ),
            twin_memory_module.CGM_TABLE: (
                _real_dataset(user_id=1, email="user-a@example.com")[twin_memory_module.CGM_TABLE]
                + [_cgm_row("user-b@example.com", f"2026-08-{d:02d}", 999) for d in range(1, 7)]
            ),
        }
        fake = _FakeSupabase(dataset)
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "user-b@example.com")
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda email: 2)

        result = await twin_memory_module.list_patterns(authorization="Bearer x")

        # User B's own Google Health steps are all constant (1) -> zero
        # variance -> _pearson returns None -> no pattern, and User A's real
        # varying data must never have leaked into User B's calculation.
        pattern_types = {row.get("pattern_type") for row in result["items"]}
        assert "aktivitaet_glukose_gleicher_tag" not in pattern_types

    async def test_family_membership_grants_no_access_to_another_members_data(self, monkeypatch):
        """No Family concept exists anywhere in this code path — proven the
        same way as User A/B isolation, since `list_patterns` only ever
        resolves the SINGLE requesting user's own email/user_id."""
        dataset = _real_dataset(user_id=10, email="family-owner@example.com")
        fake = _FakeSupabase(dataset)
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "family-member@example.com")
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda email: 11)

        result = await twin_memory_module.list_patterns(authorization="Bearer x")
        pattern_types = {row.get("pattern_type") for row in result["items"]}
        assert "aktivitaet_glukose_gleicher_tag" not in pattern_types
