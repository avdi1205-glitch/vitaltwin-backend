"""Unit tests for `app.core.account_deletion.purge_all_user_data` — the
function that actually executes an admin-triggered GDPR account deletion
across every user-data table."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import account_deletion


class _FakeQuery:
    def __init__(self, table_name: str, tables: dict[str, list[dict]]):
        self._table_name = table_name
        self._tables = tables
        self._filters: list[tuple[str, object]] = []
        self._deleting = False

    def select(self, *_a, **_k):
        return self

    def eq(self, field, value):
        self._filters.append((field, value))
        return self

    def in_(self, field, values):
        self._filters.append((field, set(values)))
        return self

    def delete(self):
        self._deleting = True
        return self

    def _matching(self) -> list[dict]:
        rows = self._tables.get(self._table_name, [])
        for field, value in self._filters:
            if isinstance(value, set):
                rows = [r for r in rows if r.get(field) in value]
            else:
                rows = [r for r in rows if r.get(field) == value]
        return rows

    def execute(self):
        matched = self._matching()
        if self._deleting:
            remaining = [r for r in self._tables.get(self._table_name, []) if r not in matched]
            self._tables[self._table_name] = remaining
        return SimpleNamespace(data=matched)


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def table(self, name):
        return _FakeQuery(name, self.tables)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_purge_deletes_across_all_tables_and_recommendation_children(monkeypatch):
    email = "user-a@example.com"
    tables = {
        account_deletion.RECOMMENDATION_TABLE: [{"id": "rec-1", "email": email}],
        account_deletion.RECOMMENDATION_DECISION_TABLE: [{"id": "d1", "recommendation_id": "rec-1"}],
        account_deletion.RECOMMENDATION_OUTCOME_TABLE: [{"id": "o1", "recommendation_id": "rec-1"}],
        account_deletion.RECOMMENDATION_FEEDBACK_TABLE: [{"id": "f1", "recommendation_id": "rec-1"}],
        account_deletion.HABIT_TABLE: [{"id": "h1", "email": email}],
        account_deletion.DAILY_ENTRY_TABLE: [{"id": "e1", "email": email}],
        account_deletion.PROFILE_TABLE: [{"email": email}],
        account_deletion.USER_TABLE: [{"email": email}],
    }
    fake = _FakeSupabase(tables)
    monkeypatch.setattr(account_deletion, "supabase", fake)

    result = account_deletion.purge_all_user_data(email)

    assert result[account_deletion.RECOMMENDATION_TABLE] == 1
    assert result[account_deletion.RECOMMENDATION_DECISION_TABLE] == 1
    assert result[account_deletion.RECOMMENDATION_OUTCOME_TABLE] == 1
    assert result[account_deletion.RECOMMENDATION_FEEDBACK_TABLE] == 1
    assert result[account_deletion.HABIT_TABLE] == 1
    assert result[account_deletion.DAILY_ENTRY_TABLE] == 1
    assert result[account_deletion.PROFILE_TABLE] == 1
    assert result[account_deletion.USER_TABLE] == 1

    assert tables[account_deletion.RECOMMENDATION_TABLE] == []
    assert tables[account_deletion.RECOMMENDATION_DECISION_TABLE] == []
    assert tables[account_deletion.USER_TABLE] == []


def test_purge_never_touches_other_users_data(monkeypatch):
    tables = {
        account_deletion.HABIT_TABLE: [
            {"id": "h1", "email": "user-a@example.com"},
            {"id": "h2", "email": "user-b@example.com"},
        ],
        account_deletion.USER_TABLE: [
            {"email": "user-a@example.com"},
            {"email": "user-b@example.com"},
        ],
    }
    fake = _FakeSupabase(tables)
    monkeypatch.setattr(account_deletion, "supabase", fake)

    account_deletion.purge_all_user_data("user-a@example.com")

    assert tables[account_deletion.HABIT_TABLE] == [{"id": "h2", "email": "user-b@example.com"}]
    assert tables[account_deletion.USER_TABLE] == [{"email": "user-b@example.com"}]


def test_purge_reports_none_on_table_failure_not_fabricated_zero(monkeypatch):
    class _RaisingQuery(_FakeQuery):
        def execute(self):
            if self._table_name == account_deletion.HABIT_TABLE and self._deleting:
                raise RuntimeError("boom")
            return super().execute()

    class _RaisingSupabase(_FakeSupabase):
        def table(self, name):
            return _RaisingQuery(name, self.tables)

    fake = _RaisingSupabase({account_deletion.HABIT_TABLE: [{"id": "h1", "email": "user-a@example.com"}]})
    monkeypatch.setattr(account_deletion, "supabase", fake)

    result = account_deletion.purge_all_user_data("user-a@example.com")
    assert result[account_deletion.HABIT_TABLE] is None
