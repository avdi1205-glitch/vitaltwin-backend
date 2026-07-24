"""In-app notifications — the one notification channel that is genuinely
implemented (see `core/integrations.py`). Push/E-Mail-Newsletter are
explicitly not implemented; do not add fake senders here."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from ..core.auth import require_email
from ..core.supabase import supabase

router = APIRouter()

NOTIFICATION_TABLE = "vt_notifications"


@router.get("")
async def list_notifications(authorization: str | None = Header(default=None)):
    email = require_email(authorization)
    try:
        rows = (
            supabase.table(NOTIFICATION_TABLE)
            .select("id,title,body,read,created_at")
            .eq("email", email)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    unread_count = sum(1 for row in rows if not row.get("read"))
    return {"items": rows, "unread_count": unread_count}


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: str, authorization: str | None = Header(default=None)):
    email = require_email(authorization)
    try:
        response = (
            supabase.table(NOTIFICATION_TABLE)
            .update({"read": True})
            .eq("id", notification_id)
            .eq("email", email)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Benachrichtigung konnte nicht aktualisiert werden.") from exc

    if not response.data:
        raise HTTPException(status_code=404, detail="Benachrichtigung nicht gefunden.")
    return {"message": "Als gelesen markiert."}


def create_notification(*, email: str, title: str, body: str) -> bool:
    """Internal helper for other services to create an in-app notification
    (e.g. a future recommendation-ready or weekly-reflection-ready event).
    Best-effort, fire-and-forget — a failed notification must never block
    the action that triggered it."""
    try:
        supabase.table(NOTIFICATION_TABLE).insert({"email": email, "title": title, "body": body}).execute()
        return True
    except Exception:
        return False
