"""Affiliate Center — public/user-facing API (VitalTwin Enterprise Release
— Affiliate Intelligence & Management Platform).

Mounted at `/api/affiliate` in `app/main.py`. Two audiences:

- **Recommendations** (`GET /recommendations`): the *only* place the rest
  of the app (frontend, twin/chat) may ask "what products can I show this
  user?" — always routed through `core/affiliate_engine.py`, so the
  approved/active/not-expired/not-blacklisted/not-hidden rules are
  enforced in exactly one place.
- **Tracking + user preferences** (`POST /track`, `GET`/`PUT /prefs`):
  compliance requires every recommendation to be labelled ("Partner-
  empfehlung / Affiliate Link / Werbung" — enforced by the frontend
  rendering the `is_affiliate: true` marker every recommendation object
  carries) and every user must be able to opt out entirely.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..core.affiliate_engine import get_recommendations_for_user, get_user_prefs
from ..core.auth import require_email
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase

router = APIRouter()

EVENT_TABLE = "vt_affiliate_events"
USER_PREFS_TABLE = "vt_affiliate_user_prefs"

ALLOWED_TRACK_EVENT_TYPES = {"impression", "click", "conversion"}


def _optional_email(authorization: str | None) -> str | None:
    if not authorization:
        return None
    try:
        return require_email(authorization)
    except HTTPException:
        return None


class TrackInput(BaseModel):
    product_id: str
    event_type: str
    ab_test_id: str | None = None
    ab_test_variant: str | None = None
    revenue: float | None = None
    commission: float | None = None


class PrefsInput(BaseModel):
    affiliate_enabled: bool
    hidden_categories: list[str] = []
    hidden_products: list[str] = []


@router.get("/recommendations")
async def get_recommendations(
    category: str | None = None,
    limit: int = 10,
    authorization: str | None = Header(default=None),
):
    """Returns admin-approved, non-expired, non-blacklisted products the
    current user hasn't hidden. Every item is explicitly marked
    `"is_affiliate": true` so the frontend can never accidentally render
    an affiliate product without disclosure (Compliance §)."""
    email = require_email(authorization)
    limit = max(1, min(limit, 50))
    products = get_recommendations_for_user(email, category=category, limit=limit)
    for product in products:
        product["is_affiliate"] = True
        product["disclosure"] = "Partnerempfehlung / Affiliate Link / Werbung"
    return {"items": products}


@router.post("/track")
async def track_event(data: TrackInput, request: Request, authorization: str | None = Header(default=None)):
    """Public tracking endpoint (impressions/clicks work for anonymous
    visitors too — `email` is attached only if the caller is logged in).
    Rate-limited per client IP to reduce trivial abuse/inflation."""
    if data.event_type not in ALLOWED_TRACK_EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Ungültiger event_type. Erlaubt: {', '.join(sorted(ALLOWED_TRACK_EVENT_TYPES))}")
    enforce_rate_limit(request, "affiliate_track", max_requests=120, window_seconds=60)

    email = _optional_email(authorization)
    payload = {
        "product_id": data.product_id,
        "ab_test_id": data.ab_test_id,
        "ab_test_variant": data.ab_test_variant,
        "event_type": data.event_type,
        "email": email,
        "revenue": data.revenue,
        "commission": data.commission,
        "context": {},
    }
    try:
        supabase.table(EVENT_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ereignis konnte nicht gespeichert werden.") from exc
    return {"message": "Erfasst."}


@router.get("/prefs")
async def get_prefs(authorization: str | None = Header(default=None)):
    email = require_email(authorization)
    return get_user_prefs(email)


@router.put("/prefs")
async def update_prefs(data: PrefsInput, authorization: str | None = Header(default=None)):
    email = require_email(authorization)
    payload = {
        "email": email,
        "affiliate_enabled": data.affiliate_enabled,
        "hidden_categories": data.hidden_categories,
        "hidden_products": data.hidden_products,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table(USER_PREFS_TABLE).upsert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Einstellung konnte nicht gespeichert werden.") from exc
    return {"message": "Gespeichert."}
