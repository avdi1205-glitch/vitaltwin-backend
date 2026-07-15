import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.supabase import supabase

router = APIRouter()

APPLICATION_TABLE = "vt_beta_applications"
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,24}$")


class BetaApplicationRequest(BaseModel):
    full_name: str
    email: str
    age: int | None = None
    motivation: str
    source: str | None = None
    # Honeypot field: real users never fill this (hidden via CSS). If it has a
    # value, the submission is almost certainly an automated bot.
    website: str | None = None


def _db_store_application(data: dict[str, object]) -> bool:
    try:
        supabase.table(APPLICATION_TABLE).insert(data).execute()
        return True
    except Exception:
        return False


def _db_has_application(email: str) -> bool:
    try:
        response = (
            supabase.table(APPLICATION_TABLE)
            .select("id")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return bool(response.data)
    except Exception:
        return False


@router.post("/apply")
async def apply_for_beta(req: BetaApplicationRequest):
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

    if _db_has_application(email):
        return {
            "message": "Du hast dich bereits beworben. Wir melden uns, sobald dein Platz bestätigt ist.",
            "already_applied": True,
        }

    saved = _db_store_application(
        {
            "full_name": full_name,
            "email": email,
            "age": req.age,
            "motivation": motivation,
            "source": (req.source or "landingpage").strip()[:120],
        }
    )

    if not saved:
        raise HTTPException(
            status_code=500,
            detail="Bewerbung konnte gerade nicht gespeichert werden. Bitte versuche es in wenigen Minuten erneut.",
        )

    return {
        "message": "Danke für deine Bewerbung! Wir melden uns per E-Mail, sobald dein Platz in der Beta-Kohorte bestätigt ist.",
        "already_applied": False,
    }
