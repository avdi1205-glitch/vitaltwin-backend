import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..core.locale import resolve_locale
from ..core.supabase import supabase_admin
from ..core.rate_limit import enforce_rate_limit

router = APIRouter()

APPLICATION_TABLE = "vt_beta_applications"
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,24}$")

# Total "first 20 beta testers" discount slots (50% off, 6 months, once a
# paid plan is chosen). The actual grant-tracking table/race-safe counting
# mechanism/Stripe coupon logic is still plan-only (2026-08-21) — until it
# exists, zero grants can possibly have been given out yet, so the honest
# current answer is simply the full total.
TOTAL_DISCOUNT_SLOTS = 20

_NOT_CONFIGURED_MESSAGE = {
    "de": "Die Beta-Bewerbung ist aktuell technisch nicht verf\u00fcgbar. Bitte versuche es sp\u00e4ter erneut oder schreib uns direkt an info@vitaltwin.de.",
    "en": "The beta application is currently technically unavailable. Please try again later or write to us directly at info@vitaltwin.de.",
}


class BetaApplicationRequest(BaseModel):
    full_name: str
    email: str
    age: int | None = None
    motivation: str
    source: str | None = None
    # Honeypot field: real users never fill this (hidden via CSS). If it has a
    # value, the submission is almost certainly an automated bot.
    website: str | None = None


def _require_admin_client(locale: str):
    """`vt_beta_applications` has RLS enabled with zero anon policies
    (migration 040) — only the privileged server client may touch it. Fails
    closed (503, never a fake/degraded success) if
    `SUPABASE_SERVICE_ROLE_KEY` hasn't been configured yet, rather than
    silently falling back to the anon client (which would just reproduce
    the exact PII-exposure risk this design fixes)."""
    if supabase_admin is None:
        raise HTTPException(status_code=503, detail=_NOT_CONFIGURED_MESSAGE[locale])
    return supabase_admin


def _db_store_application(data: dict[str, object], locale: str) -> bool:
    try:
        _require_admin_client(locale).table(APPLICATION_TABLE).insert(data).execute()
        return True
    except HTTPException:
        raise
    except Exception:
        return False


def _db_has_application(email: str, locale: str) -> bool:
    try:
        response = (
            _require_admin_client(locale)
            .table(APPLICATION_TABLE)
            .select("id")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return bool(response.data)
    except HTTPException:
        raise
    except Exception:
        return False


def _db_application_status(email: str, locale: str) -> str | None:
    """Public, honest status lookup for the customer-facing application page
    (migration 039's `status` column) — never invents a status for an
    email that never applied."""
    try:
        response = (
            _require_admin_client(locale)
            .table(APPLICATION_TABLE)
            .select("status")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return str(rows[0]["status"]) if rows else None
    except HTTPException:
        raise
    except Exception:
        return None


@router.post("/apply")
async def apply_for_beta(req: BetaApplicationRequest, request: Request, locale: str | None = None):
    resolved_locale = resolve_locale(locale)
    enforce_rate_limit(request, "beta_apply", max_requests=5, window_seconds=60)
    # Silently pretend success for bots so they don't learn to adapt.
    if req.website:
        return {
            "message": "Danke für deine Bewerbung! Wir melden uns per E-Mail, sobald dein Platz in der Beta-Kohorte bestätigt ist.",
            "already_applied": False,
        }

    full_name = req.full_name.strip()
    email = req.email.strip().lower()
    motivation = req.motivation.strip()

    if not (2 <= len(full_name) <= 200):
        raise HTTPException(status_code=400, detail="Bitte gib deinen vollständigen Namen ein")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Bitte gib eine gültige E-Mail-Adresse ein")
    if not (10 <= len(motivation) <= 2000):
        raise HTTPException(
            status_code=400,
            detail="Bitte beschreibe kurz (10-2000 Zeichen), warum du an der Beta teilnehmen möchtest",
        )
    if req.age is not None and not (16 <= req.age <= 100):
        raise HTTPException(status_code=400, detail="Bitte gib ein gültiges Alter an")

    if _db_has_application(email, resolved_locale):
        return {
            "message": "Du hast dich bereits beworben. Wir melden uns, sobald dein Platz bestätigt ist.",
            "already_applied": True,
            "status": _db_application_status(email, resolved_locale),
        }

    saved = _db_store_application(
        {
            "full_name": full_name,
            "email": email,
            "age": req.age,
            "motivation": motivation,
            "source": (req.source or "landingpage").strip()[:120],
        },
        resolved_locale,
    )

    if not saved:
        raise HTTPException(
            status_code=500,
            detail="Bewerbung konnte gerade nicht gespeichert werden. Bitte versuche es in wenigen Minuten erneut.",
        )

    return {
        "message": "Danke für deine Bewerbung! Wir melden uns per E-Mail, sobald dein Platz in der Beta-Kohorte bestätigt ist.",
        "already_applied": False,
        "status": "pending",
    }


@router.get("/status")
async def beta_application_status(email: str, request: Request, locale: str | None = None):
    """Public, honest status check for the customer-facing application page
    — lets an applicant come back later (without logging in) and see
    pending/approved/rejected instead of re-applying. Never reveals whether
    ANY OTHER email applied — only ever returns state for the exact email
    the caller already knows and provides themselves."""
    resolved_locale = resolve_locale(locale)
    enforce_rate_limit(request, "beta_status", max_requests=20, window_seconds=60)
    normalized_email = email.strip().lower()
    if not _EMAIL_RE.match(normalized_email):
        raise HTTPException(status_code=400, detail="Bitte gib eine gültige E-Mail-Adresse ein")
    status = _db_application_status(normalized_email, resolved_locale)
    return {"applied": status is not None, "status": status}


@router.get("/discount-slots-remaining")
async def beta_discount_slots_remaining(request: Request):
    """Public, no-auth endpoint feeding the "first 20 beta testers" 50%-
    discount counter shown on the homepage and /preise. No user data of
    any kind is exposed or required — just a plain integer. See the
    `TOTAL_DISCOUNT_SLOTS` module comment for why this is currently a
    constant rather than a real grant count."""
    enforce_rate_limit(request, "beta_discount_slots", max_requests=60, window_seconds=60)
    return {"remaining_slots": TOTAL_DISCOUNT_SLOTS, "total_slots": TOTAL_DISCOUNT_SLOTS}
