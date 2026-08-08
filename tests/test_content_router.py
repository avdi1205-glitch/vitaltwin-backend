"""Unit tests for `app.routers.content` — the public (no-auth) read-only
blog endpoints. Must NEVER return draft/archived content items."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import content as content_module


class _FakeQuery:
    def __init__(self, rows: list[dict], total: int | None):
        self._rows = rows
        self._total = total
        self._filters: list[tuple[str, object]] = []

    def select(self, *_a, count=None, **_k):
        return self

    def eq(self, field, value):
        self._filters.append((field, value))
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def _matching(self):
        rows = self._rows
        for field, value in self._filters:
            rows = [r for r in rows if r.get(field) == value]
        return rows

    def execute(self):
        matched = self._matching()
        return SimpleNamespace(data=matched, count=self._total if self._total is not None else len(matched))


class _FakeSupabase:
    def __init__(self, rows: list[dict], total: int | None = None):
        self.rows = rows
        self.total = total

    def table(self, _name):
        return _FakeQuery(self.rows, self.total)


@pytest.fixture
def anyio_backend():
    return "asyncio"


PUBLISHED_ROW = {
    "slug": "was-ist-ein-digitaler-wellness-zwilling",
    "title": "Was ist ein digitaler Wellness-Zwilling?",
    "body": "Ein digitaler Wellness-Zwilling ist ...",
    "content_type": "blog",
    "status": "published",
    "published_at": "2026-08-01T00:00:00Z",
}

DRAFT_ROW = {**PUBLISHED_ROW, "slug": "draft-artikel", "status": "draft"}


@pytest.mark.anyio
async def test_list_only_returns_published_items(monkeypatch):
    fake = _FakeSupabase([PUBLISHED_ROW, DRAFT_ROW])
    monkeypatch.setattr(content_module, "supabase", fake)

    result = await content_module.list_published_blog_posts()
    assert len(result["items"]) == 1
    assert result["items"][0]["slug"] == "was-ist-ein-digitaler-wellness-zwilling"


@pytest.mark.anyio
async def test_list_skips_rows_without_a_slug(monkeypatch):
    no_slug_row = {**PUBLISHED_ROW, "slug": None}
    fake = _FakeSupabase([no_slug_row])
    monkeypatch.setattr(content_module, "supabase", fake)

    result = await content_module.list_published_blog_posts()
    assert result["items"] == []


@pytest.mark.anyio
async def test_get_single_post_by_slug(monkeypatch):
    fake = _FakeSupabase([PUBLISHED_ROW])
    monkeypatch.setattr(content_module, "supabase", fake)

    result = await content_module.get_published_blog_post("was-ist-ein-digitaler-wellness-zwilling")
    assert result["title"] == PUBLISHED_ROW["title"]


@pytest.mark.anyio
async def test_get_single_post_404_when_not_published(monkeypatch):
    fake = _FakeSupabase([DRAFT_ROW])
    monkeypatch.setattr(content_module, "supabase", fake)

    with pytest.raises(HTTPException) as exc_info:
        await content_module.get_published_blog_post("draft-artikel")
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_list_handles_db_failure_honestly(monkeypatch):
    class _RaisingSupabase:
        def table(self, _name):
            raise RuntimeError("boom")

    monkeypatch.setattr(content_module, "supabase", _RaisingSupabase())
    result = await content_module.list_published_blog_posts()
    assert result["items"] == []
    assert result["total"] == 0
