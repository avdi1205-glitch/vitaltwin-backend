"""Tests for `GET /api/profile/twin-evolution` (Twin Core Phase 7). Mocks
Supabase and `get_user_id_by_email` — no real network/database access."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.routers import profile as profile_module

TODAY = date(2026, 8, 11)


class _Query:
    def __init__(self, table_name, dataset, calls_log):
        self._table = table_name
        self._dataset = dataset
        self._calls_log = calls_log
        self._filters: dict[str, object] = {}
        self._insert_payload = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._calls_log.append((self._table, field, value))
        self._filters[field] = value
        return self

    def gte(self, *args, **kwargs):
        return self

    def lte(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
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

    def _matching(self, rows):
        return [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]

    def execute(self):
        rows = self._dataset.setdefault(self._table, [])
        if self._insert_payload is not None:
            row = dict(self._insert_payload)
            row.setdefault("id", f"snap-{len(rows)}")
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matching = self._matching(rows)
        matching = sorted(matching, key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return SimpleNamespace(data=[dict(r) for r in matching])


class _FakeSupabase:
    def __init__(self, dataset: dict[str, list[dict]] | None = None):
        self.dataset = dataset or {}
        self.calls: list[tuple[str, str, object]] = []

    def table(self, name):
        return _Query(name, self.dataset, self.calls)


def _checkin_entries(email: str, days: int, sleep_avg: float) -> list[dict]:
    return [
        {"email": email, "entry_date": (TODAY - timedelta(days=i)).isoformat(), "sleep_hours": sleep_avg}
        for i in range(days)
    ]


@pytest.mark.anyio
class TestTwinEvolutionEndpoint:
    async def test_empty_state_is_not_persisted(self, monkeypatch):
        fake = _FakeSupabase({})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda email: None)

        result = await profile_module.get_twin_evolution(authorization="Bearer x")
        assert result["available"] is False
        assert result["snapshot_recorded"] is False
        assert result["comparison"]["available"] is False

    async def test_first_meaningful_snapshot_is_persisted(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase({profile_module.DAILY_ENTRY_TABLE: _checkin_entries(email, 5, 7.0)})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda e: None)

        result = await profile_module.get_twin_evolution(authorization="Bearer x")
        assert result["available"] is True
        assert result["snapshot_recorded"] is True
        assert result["comparison"]["available"] is False  # no earlier snapshot to compare against
        snapshots = fake.dataset.get(profile_module.SNAPSHOT_TABLE, [])
        assert len(snapshots) == 1
        assert snapshots[0]["email"] == email
        assert snapshots[0]["snapshot_version"] == profile_module.SNAPSHOT_VERSION

    async def test_no_duplicate_snapshot_for_unchanged_state_same_day(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase({profile_module.DAILY_ENTRY_TABLE: _checkin_entries(email, 5, 7.0)})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda e: None)

        await profile_module.get_twin_evolution(authorization="Bearer x")
        second = await profile_module.get_twin_evolution(authorization="Bearer x")

        assert second["snapshot_recorded"] is False
        assert len(fake.dataset.get(profile_module.SNAPSHOT_TABLE, [])) == 1

    async def test_meaningful_change_produces_a_real_comparison_explanation(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase({profile_module.DAILY_ENTRY_TABLE: _checkin_entries(email, 5, 6.0)})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda e: None)
        await profile_module.get_twin_evolution(authorization="Bearer x")

        # Backdate the stored snapshot so the same-day noise gate doesn't
        # suppress a second, genuinely different snapshot.
        fake.dataset[profile_module.SNAPSHOT_TABLE][0]["created_at"] = "2026-08-01T09:00:00+00:00"
        fake.dataset[profile_module.DAILY_ENTRY_TABLE] = _checkin_entries(email, 5, 8.0)

        result = await profile_module.get_twin_evolution(authorization="Bearer x")
        assert result["snapshot_recorded"] is True
        assert result["comparison"]["available"] is True
        assert any("gestiegen" in text for text in result["comparison"]["explanations"])

    async def test_response_never_exposes_raw_internal_snapshot_terminology(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase({profile_module.DAILY_ENTRY_TABLE: _checkin_entries(email, 5, 7.0)})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda e: None)

        result = await profile_module.get_twin_evolution(authorization="Bearer x")
        assert "domains" not in result
        assert "snapshot" not in result

    async def test_user_a_snapshot_never_appears_for_user_b(self, monkeypatch):
        fake = _FakeSupabase(
            {profile_module.DAILY_ENTRY_TABLE: _checkin_entries("user-a@example.com", 5, 7.0)}
        )
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda e: None)

        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")
        await profile_module.get_twin_evolution(authorization="Bearer x")

        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-b@example.com")
        result_b = await profile_module.get_twin_evolution(authorization="Bearer x")
        assert result_b["available"] is False
        assert result_b["snapshot_recorded"] is False

    async def test_family_membership_grants_no_cross_access(self, monkeypatch):
        """No Family table is touched anywhere in this endpoint -- each
        request only ever resolves the single requesting user's own email."""
        fake = _FakeSupabase(
            {profile_module.DAILY_ENTRY_TABLE: _checkin_entries("family-owner@example.com", 5, 7.0)}
        )
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda e: None)

        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "family-owner@example.com")
        await profile_module.get_twin_evolution(authorization="Bearer x")

        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "family-member@example.com")
        result_member = await profile_module.get_twin_evolution(authorization="Bearer x")
        assert result_member["available"] is False

    async def test_wellness_only_disclaimer_present_no_medical_wording(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase({profile_module.DAILY_ENTRY_TABLE: _checkin_entries(email, 5, 7.0)})
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(profile_module, "get_user_id_by_email", lambda e: None)

        result = await profile_module.get_twin_evolution(authorization="Bearer x")
        assert "Diagnose" in result["disclaimer"]
        for forbidden in ("Risiko", "Krankheit", "Therapieempfehlung"):
            assert forbidden not in result["disclaimer"]
