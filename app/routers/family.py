"""Family Foundation V1 ("Vollständiger erweiterter digitaler Zwilling" ->
Family tier, multi-profile membership only).

Constitution-critical scope limit (see task spec): this module ONLY builds
account membership — who belongs to which Family group, with which role
and status. It does NOT grant any member (owner included) automatic access
to another member's private wellness data (check-ins, CGM/nutrition,
Google Health, Twin chat, goals, habits, reports, simulations). Each
member remains a fully independent VitalTwin account; membership is purely
an identity/roster relationship. Shared challenges, family goals, and any
cross-member data visibility are explicitly OUT OF SCOPE for this file and
must not be added here without a separate, deliberate task.

Reuses `core/auth.py::require_user`/`get_user_id_by_email` (no parallel
identity resolution), `core/plan_service.py::has_feature` (the
`family_profiles` feature — Family tier only), and the same best-effort
SMTP pattern already used by `routers/contact.py` (no new email service).
"""

from __future__ import annotations

import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.auth import get_user_id_by_email, require_user
from ..core.plan_service import MAX_FAMILY_MEMBERS, has_feature
from ..core.supabase import supabase

router = APIRouter()

FAMILY_TABLE = "vt_families"
MEMBER_TABLE = "vt_family_members"
USER_TABLE = "vt_users"
PROFILE_TABLE = "vt_user_profiles"

OPEN_STATUSES = ("active", "invited")

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,24}$")


class InviteRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _EMAIL_RE.match(normalized):
            raise ValueError("Ungültige E-Mail-Adresse.")
        return normalized


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_open_membership(user_id: int) -> dict[str, object] | None:
    """The caller's own membership row (any Family), if it's in an
    active/invited state — a user may have at most one at a time (also
    enforced at the database level, see migration 029)."""
    try:
        response = (
            supabase.table(MEMBER_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .in_("status", OPEN_STATUSES)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def _count_active_members(family_id: int) -> int:
    try:
        response = (
            supabase.table(MEMBER_TABLE)
            .select("id")
            .eq("family_id", family_id)
            .eq("status", "active")
            .execute()
        )
        return len(response.data or [])
    except Exception:
        return 0


def _list_members(family_id: int) -> list[dict[str, object]]:
    try:
        rows = (
            supabase.table(MEMBER_TABLE)
            .select("*")
            .eq("family_id", family_id)
            .in_("status", OPEN_STATUSES)
            .order("created_at")
            .execute()
            .data
            or []
        )
    except Exception:
        return []

    user_ids = [row["user_id"] for row in rows]
    if not user_ids:
        return []

    try:
        user_rows = supabase.table(USER_TABLE).select("id,email").in_("id", user_ids).execute().data or []
    except Exception:
        user_rows = []
    email_by_id = {int(u["id"]): str(u["email"]) for u in user_rows}

    display_name_by_email: dict[str, str | None] = {}
    emails = list(email_by_id.values())
    if emails:
        try:
            profile_rows = (
                supabase.table(PROFILE_TABLE).select("email,display_name").in_("email", emails).execute().data or []
            )
            display_name_by_email = {p["email"]: p.get("display_name") for p in profile_rows}
        except Exception:
            display_name_by_email = {}

    members = []
    for row in rows:
        uid = int(row["user_id"])
        email = email_by_id.get(uid, "")
        members.append({
            "user_id": uid,
            "email": email,
            "display_name": display_name_by_email.get(email),
            "role": row.get("role"),
            "status": row.get("status"),
        })
    return members


def _membership_response(email: str, user_id: int) -> dict[str, object]:
    membership = _get_open_membership(user_id)
    if not membership:
        return {
            "in_family": False,
            "eligible_to_create": has_feature(email, "family_profiles"),
            "family_id": None,
            "role": None,
            "status": None,
            "member_count_active": 0,
            "max_members": MAX_FAMILY_MEMBERS,
            "members": [],
        }

    family_id = int(membership["family_id"])
    return {
        "in_family": True,
        "eligible_to_create": has_feature(email, "family_profiles"),
        "family_id": family_id,
        "role": membership.get("role"),
        "status": membership.get("status"),
        "member_count_active": _count_active_members(family_id),
        "max_members": MAX_FAMILY_MEMBERS,
        "members": _list_members(family_id),
    }


def _send_invite_email(to_email: str, inviter_display: str) -> bool:
    """Best-effort, same SMTP env vars already used by
    `routers/contact.py` — no new dependency, no new external service.
    Returns whether an email was actually sent, so the caller can be
    honest with the owner if delivery wasn't possible (Constitution rule
    19: no fake confirmation)."""
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    if not (smtp_host and smtp_user and smtp_password):
        return False

    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    msg = EmailMessage()
    msg["Subject"] = "Einladung zu einer VitalTwin Family"
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.set_content(
        f"{inviter_display} hat dich zu ihrer/seiner VitalTwin Family eingeladen.\n\n"
        "Logge dich bei VitalTwin ein und öffne 'Profil' -> 'Familie', um die Einladung anzunehmen."
    )
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception:
        return False


@router.get("/me")
async def get_my_family(authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    return _membership_response(current.email, current.user_id)


@router.post("")
async def create_family(authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    if not has_feature(current.email, "family_profiles"):
        raise HTTPException(
            status_code=403,
            detail="Eine Family ist ein Family-Tarif-Feature. Upgrade auf Family, um eine Family zu erstellen.",
        )
    if _get_open_membership(current.user_id):
        raise HTTPException(status_code=409, detail="Du bist bereits Teil einer Family.")

    try:
        family_response = supabase.table(FAMILY_TABLE).insert({"owner_user_id": current.user_id}).execute()
        family_row = family_response.data[0] if family_response.data else None
        if not family_row:
            raise HTTPException(status_code=500, detail="Family konnte nicht erstellt werden.")
        family_id = int(family_row["id"])
        supabase.table(MEMBER_TABLE).insert({
            "family_id": family_id,
            "user_id": current.user_id,
            "role": "owner",
            "status": "active",
        }).execute()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Family konnte nicht erstellt werden.") from exc

    return _membership_response(current.email, current.user_id)


@router.post("/invite")
async def invite_member(data: InviteRequest, authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")

    membership = _get_open_membership(current.user_id)
    if not membership or membership.get("role") != "owner" or membership.get("status") != "active":
        raise HTTPException(status_code=403, detail="Nur der Family-Owner kann Mitglieder einladen.")

    invitee_user_id = get_user_id_by_email(data.email)
    if invitee_user_id is None:
        raise HTTPException(status_code=404, detail="Kein VitalTwin-Konto mit dieser E-Mail gefunden.")
    if invitee_user_id == current.user_id:
        raise HTTPException(status_code=400, detail="Du kannst dich nicht selbst einladen.")
    if _get_open_membership(invitee_user_id):
        raise HTTPException(status_code=409, detail="Diese Person ist bereits Teil einer Family.")

    family_id = int(membership["family_id"])
    if _count_active_members(family_id) >= MAX_FAMILY_MEMBERS:
        raise HTTPException(
            status_code=409, detail=f"Maximale Mitgliederzahl ({MAX_FAMILY_MEMBERS}) bereits erreicht."
        )

    # A previously removed/left member already has a row for this exact
    # (family_id, user_id) pair — migration 029's `unique(family_id,
    # user_id)` constraint means re-inviting them must UPDATE that row
    # back to 'invited', never INSERT a second one (which would violate
    # the constraint and fail with a real database error).
    try:
        existing_rows = (
            supabase.table(MEMBER_TABLE)
            .select("id")
            .eq("family_id", family_id)
            .eq("user_id", invitee_user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        existing_rows = []

    try:
        if existing_rows:
            supabase.table(MEMBER_TABLE).update({
                "role": "member",
                "status": "invited",
                "updated_at": _now_iso(),
            }).eq("id", existing_rows[0]["id"]).execute()
        else:
            supabase.table(MEMBER_TABLE).insert({
                "family_id": family_id,
                "user_id": invitee_user_id,
                "role": "member",
                "status": "invited",
            }).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Einladung konnte nicht gespeichert werden.") from exc

    email_sent = _send_invite_email(data.email, current.email)
    return {"invited": True, "email_sent": email_sent}


@router.post("/accept")
async def accept_invitation(authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")

    membership = _get_open_membership(current.user_id)
    if not membership or membership.get("status") != "invited":
        raise HTTPException(status_code=404, detail="Keine offene Einladung gefunden.")

    family_id = int(membership["family_id"])
    if _count_active_members(family_id) >= MAX_FAMILY_MEMBERS:
        raise HTTPException(
            status_code=409, detail=f"Die Family hat bereits die maximale Mitgliederzahl ({MAX_FAMILY_MEMBERS}) erreicht."
        )

    try:
        supabase.table(MEMBER_TABLE).update({"status": "active", "updated_at": _now_iso()}).eq(
            "id", membership["id"]
        ).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Einladung konnte nicht angenommen werden.") from exc

    return _membership_response(current.email, current.user_id)


@router.post("/leave")
async def leave_family(authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")

    membership = _get_open_membership(current.user_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Du bist aktuell kein Mitglied einer Family.")

    if membership.get("role") == "owner":
        family_id = int(membership["family_id"])
        other_active = [m for m in _list_members(family_id) if m["user_id"] != current.user_id]
        if other_active:
            raise HTTPException(
                status_code=409,
                detail="Als Owner musst du zuerst alle anderen Mitglieder entfernen, bevor du die Family verlässt.",
            )

    try:
        supabase.table(MEMBER_TABLE).update({"status": "left", "updated_at": _now_iso()}).eq(
            "id", membership["id"]
        ).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Family konnte nicht verlassen werden.") from exc

    return {"left": True}


@router.delete("/members/{user_id}")
async def remove_member(user_id: int, authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")

    owner_membership = _get_open_membership(current.user_id)
    if not owner_membership or owner_membership.get("role") != "owner" or owner_membership.get("status") != "active":
        raise HTTPException(status_code=403, detail="Nur der Family-Owner kann Mitglieder entfernen.")

    if user_id == current.user_id:
        raise HTTPException(status_code=400, detail="Der Owner kann sich nicht selbst entfernen.")

    family_id = int(owner_membership["family_id"])
    try:
        target_rows = (
            supabase.table(MEMBER_TABLE)
            .select("id")
            .eq("family_id", family_id)
            .eq("user_id", user_id)
            .in_("status", OPEN_STATUSES)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        target_rows = []
    if not target_rows:
        raise HTTPException(status_code=404, detail="Mitglied nicht gefunden.")

    try:
        supabase.table(MEMBER_TABLE).update({"status": "removed", "updated_at": _now_iso()}).eq(
            "id", target_rows[0]["id"]
        ).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Mitglied konnte nicht entfernt werden.") from exc

    return {"removed": True}
