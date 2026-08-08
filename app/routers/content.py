"""Public, read-only access to PUBLISHED content items (currently: blog
posts) authored via the existing admin CMS (`routers/admin.py`'s
`/api/admin/content` CRUD, `vt_content_items` table). No auth required —
this is the public-facing counterpart used by the marketing site's /blog
pages. Only ever returns rows with `status == "published"`; drafts and
archived items are never exposed here, matching the "AI erstellt Entwurf ->
Qualitätsprüfung -> Founder Approval -> Veröffentlichung" workflow (a
founder must explicitly flip status to "published" in the admin UI before
anything appears here)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.supabase import supabase

router = APIRouter()

CONTENT_TABLE = "vt_content_items"
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 20


@router.get("/blog")
async def list_published_blog_posts(page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    start = (page - 1) * page_size
    end = start + page_size - 1

    try:
        response = (
            supabase.table(CONTENT_TABLE)
            .select("slug,title,body,excerpt,published_at,updated_at", count="exact")
            .eq("content_type", "blog")
            .eq("status", "published")
            .order("published_at", desc=True)
            .range(start, end)
            .execute()
        )
        items = [row for row in (response.data or []) if row.get("slug")]
        total = response.count or 0
    except Exception:
        items = []
        total = 0

    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.get("/blog/{slug}")
async def get_published_blog_post(slug: str):
    try:
        rows = (
            supabase.table(CONTENT_TABLE)
            .select("slug,title,body,excerpt,published_at,updated_at")
            .eq("content_type", "blog")
            .eq("status", "published")
            .eq("slug", slug)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Artikel nicht gefunden.")
    return rows[0]
