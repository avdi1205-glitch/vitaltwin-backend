import os
import re
import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..core.supabase import supabase
from ..core.rate_limit import enforce_rate_limit

router = APIRouter()

CONTACT_TABLE = "vt_contact_messages"
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,24}$")


class ContactMessageRequest(BaseModel):
    full_name: str
    email: str
    subject: str | None = None
    message: str
    # Honeypot field: real users never fill this (hidden via CSS). If it has a
    # value, the submission is almost certainly an automated bot.
    website: str | None = None


def _db_store_message(data: dict[str, object]) -> bool:
    try:
        supabase.table(CONTACT_TABLE).insert(data).execute()
        return True
    except Exception:
        return False


def _send_notification_email(full_name: str, email: str, subject: str | None, message: str) -> None:
    """Best-effort: forwards the contact message as a real email to the
    inbox configured via CONTACT_NOTIFY_EMAIL (e.g. info@vitaltwin.de), so
    the IONOS "KI-Mail-Assistent" already active on that mailbox can see and
    optionally auto-answer simple questions. Silently does nothing if SMTP
    isn't configured — the message is still safely stored in the database
    either way, so a missing/broken SMTP setup never blocks the submission."""
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    notify_to = os.getenv("CONTACT_NOTIFY_EMAIL", "").strip() or smtp_user
    if not (smtp_host and smtp_user and smtp_password and notify_to):
        return

    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")

    email_msg = EmailMessage()
    email_msg["Subject"] = f"[Kontaktformular] {subject or 'Neue Nachricht'}"
    email_msg["From"] = smtp_user
    email_msg["To"] = notify_to
    email_msg["Reply-To"] = email
    email_msg.set_content(f"Von: {full_name} <{email}>\n\n{message}")

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(email_msg)
    except Exception:
        # Best-effort only — the message is already safely stored in the DB.
        pass


@router.post("/send")
async def send_contact_message(req: ContactMessageRequest, request: Request):
    enforce_rate_limit(request, "contact_send", max_requests=5, window_seconds=60)
    # Silently pretend success for bots so they don't learn to adapt.
    if req.website:
        return {"message": "Danke für deine Nachricht! Wir melden uns per E-Mail bei dir."}

    full_name = req.full_name.strip()
    email = req.email.strip().lower()
    message = req.message.strip()
    subject = (req.subject or "").strip()[:200] or None

    if not (2 <= len(full_name) <= 200):
        raise HTTPException(status_code=400, detail="Bitte gib deinen vollständigen Namen ein")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Bitte gib eine gültige E-Mail-Adresse ein")
    if not (10 <= len(message) <= 3000):
        raise HTTPException(
            status_code=400,
            detail="Bitte schreib eine Nachricht mit 10-3000 Zeichen",
        )

    saved = _db_store_message(
        {
            "full_name": full_name,
            "email": email,
            "subject": subject,
            "message": message,
            "source": "kontakt-page",
        }
    )

    if not saved:
        raise HTTPException(
            status_code=500,
            detail="Nachricht konnte gerade nicht gesendet werden. Bitte schreib uns direkt an info@vitaltwin.de.",
        )

    _send_notification_email(full_name, email, subject, message)

    return {
        "message": "Danke für deine Nachricht! Wir melden uns so schnell wie möglich per E-Mail bei dir.",
    }
