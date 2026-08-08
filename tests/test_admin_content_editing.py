"""Unit tests for the new content-editing admin endpoints (single-item GET,
slug-uniqueness check, publish/unpublish) — uses a fake Supabase that
actually filters by `.eq()`/`.neq()` (the shared fake in
test_admin_router.py doesn't), since these endpoints' correctness depends
on real filtering (e.g. excluding the item's own row from a slug-conflict
check)."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.admin_rbac import AdminPrincipal
from app.routers import admin as admin_module
from app.routers.admin import ContentInput


class _FilteringQuery:
    def __init__(self, table_name: str, rows: list[dict]):
        self._table_name = table_name
        self._rows = rows
        self._filters: list[tuple[str, str, object]] = []
        self._payload: dict | None = None
        self._op: str | None = None
        self._deleting = False

    def select(self, *_a, **_k):
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def neq(self, field, value):
        self._filters.append(("neq", field, value))
        return self

    def limit(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def update(self, payload):
        self._payload = payload
        self._op = "update"
        return self

    def insert(self, payload):
        self._payload = payload
        self._op = "insert"
        return self

    def delete(self):
        self._deleting = True
        return self

    def _matching(self) -> list[dict]:
        rows = self._rows
        for op, field, value in self._filters:
            if op == "eq":
                rows = [r for r in rows if r.get(field) == value]
            else:
                rows = [r for r in rows if r.get(field) != value]
        return rows

    def execute(self):
        if self._op == "insert":
            new_row = {"id": "new-id", **self._payload}
            self._rows.append(new_row)
            return SimpleNamespace(data=[new_row])
        matched = self._matching()
        if self._op == "update":
            for row in matched:
                row.update(self._payload)
            return SimpleNamespace(data=matched)
        if self._deleting:
            remaining = [r for r in self._rows if r not in matched]
            self._rows[:] = remaining
            return SimpleNamespace(data=matched)
        return SimpleNamespace(data=matched)


class _FilteringSupabase:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def table(self, name):
        return _FilteringQuery(name, self.rows)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def super_admin_principal():
    return AdminPrincipal(email="admin@example.com", role="super_admin")


@pytest.fixture
def permission_spy(monkeypatch, super_admin_principal):
    monkeypatch.setattr(admin_module, "require_admin_permission", lambda authorization, permission: super_admin_principal)


@pytest.fixture
def recorded_audit_events(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(admin_module, "record_audit_event", lambda **kwargs: events.append(kwargs))
    return events


DRAFT_ROW = {
    "id": "draft-1",
    "content_type": "blog",
    "slug": "mein-artikel",
    "title": "Mein Artikel",
    "body": "Ein echter Textkörper.",
    "status": "draft",
    "created_by": "seed-script",
}


@pytest.mark.anyio
async def test_get_content_item_found(monkeypatch, permission_spy):
    fake = _FilteringSupabase([copy.deepcopy(DRAFT_ROW)])
    monkeypatch.setattr(admin_module, "supabase", fake)

    result = await admin_module.get_content_item("draft-1", authorization="Bearer x")
    assert result["title"] == "Mein Artikel"


@pytest.mark.anyio
async def test_get_content_item_404_when_missing(monkeypatch, permission_spy):
    fake = _FilteringSupabase([])
    monkeypatch.setattr(admin_module, "supabase", fake)

    with pytest.raises(HTTPException) as exc_info:
        await admin_module.get_content_item("does-not-exist", authorization="Bearer x")
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_create_content_rejects_duplicate_slug(monkeypatch, permission_spy, recorded_audit_events):
    fake = _FilteringSupabase([copy.deepcopy(DRAFT_ROW)])
    monkeypatch.setattr(admin_module, "supabase", fake)

    with pytest.raises(HTTPException) as exc_info:
        await admin_module.create_content(
            ContentInput(content_type="blog", title="Zweiter Artikel", slug="mein-artikel"), authorization="Bearer x"
        )
    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_update_content_allows_keeping_its_own_slug(monkeypatch, permission_spy, recorded_audit_events):
    """Regression: the slug-conflict check must exclude the item's own row,
    otherwise saving an unrelated field on an already-slugged item would
    always incorrectly report a conflict with itself."""
    fake = _FilteringSupabase([copy.deepcopy(DRAFT_ROW)])
    monkeypatch.setattr(admin_module, "supabase", fake)

    result = await admin_module.update_content(
        "draft-1",
        ContentInput(content_type="blog", title="Aktualisierter Titel", slug="mein-artikel", body="Neuer Text"),
        authorization="Bearer x",
    )
    assert result["title"] == "Aktualisierter Titel"
    assert recorded_audit_events[-1]["action"] == "update"


@pytest.mark.anyio
async def test_update_content_rejects_slug_taken_by_another_item(monkeypatch, permission_spy):
    other_row = {**copy.deepcopy(DRAFT_ROW), "id": "other-id", "slug": "anderer-slug"}
    fake = _FilteringSupabase([copy.deepcopy(DRAFT_ROW), other_row])
    monkeypatch.setattr(admin_module, "supabase", fake)

    with pytest.raises(HTTPException) as exc_info:
        await admin_module.update_content(
            "draft-1", ContentInput(content_type="blog", title="x", slug="anderer-slug"), authorization="Bearer x"
        )
    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_publish_content_succeeds_when_ready(monkeypatch, permission_spy, recorded_audit_events):
    fake = _FilteringSupabase([copy.deepcopy(DRAFT_ROW)])
    monkeypatch.setattr(admin_module, "supabase", fake)

    result = await admin_module.publish_content("draft-1", authorization="Bearer x")
    assert result["status"] == "published"
    assert result["published_at"] is not None
    assert recorded_audit_events[-1]["action"] == "publish"


@pytest.mark.anyio
async def test_publish_content_fails_without_slug(monkeypatch, permission_spy):
    incomplete_row = {**copy.deepcopy(DRAFT_ROW), "slug": None}
    fake = _FilteringSupabase([incomplete_row])
    monkeypatch.setattr(admin_module, "supabase", fake)

    with pytest.raises(HTTPException) as exc_info:
        await admin_module.publish_content("draft-1", authorization="Bearer x")
    assert exc_info.value.status_code == 422
    assert "Slug" in exc_info.value.detail


@pytest.mark.anyio
async def test_publish_content_fails_without_body(monkeypatch, permission_spy):
    incomplete_row = {**copy.deepcopy(DRAFT_ROW), "body": None}
    fake = _FilteringSupabase([incomplete_row])
    monkeypatch.setattr(admin_module, "supabase", fake)

    with pytest.raises(HTTPException) as exc_info:
        await admin_module.publish_content("draft-1", authorization="Bearer x")
    assert exc_info.value.status_code == 422


@pytest.mark.anyio
async def test_unpublish_content_reverts_to_draft(monkeypatch, permission_spy, recorded_audit_events):
    published_row = {**copy.deepcopy(DRAFT_ROW), "status": "published", "published_at": "2026-08-01T00:00:00Z"}
    fake = _FilteringSupabase([published_row])
    monkeypatch.setattr(admin_module, "supabase", fake)

    result = await admin_module.unpublish_content("draft-1", authorization="Bearer x")
    assert result["status"] == "draft"
    # published_at is preserved as history, never cleared.
    assert result["published_at"] == "2026-08-01T00:00:00Z"
    assert recorded_audit_events[-1]["action"] == "unpublish"


@pytest.mark.anyio
async def test_unpublish_content_404_when_missing(monkeypatch, permission_spy):
    fake = _FilteringSupabase([])
    monkeypatch.setattr(admin_module, "supabase", fake)

    with pytest.raises(HTTPException) as exc_info:
        await admin_module.unpublish_content("does-not-exist", authorization="Bearer x")
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_list_content_filters_by_status(monkeypatch, permission_spy):
    fake = _FilteringSupabase(
        [copy.deepcopy(DRAFT_ROW), {**copy.deepcopy(DRAFT_ROW), "id": "d2", "slug": "s2", "status": "published"}]
    )
    monkeypatch.setattr(admin_module, "supabase", fake)

    result = await admin_module.list_content(status="published", authorization="Bearer x")
    assert len(result["items"]) == 1
    assert result["items"][0]["status"] == "published"
