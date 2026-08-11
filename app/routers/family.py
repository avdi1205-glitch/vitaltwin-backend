"""Family Foundation (membership) + Family Goals V1 (shared coordination
goals, Family tier).

Constitution-critical scope limit (see task spec): membership is purely an
identity/roster relationship — it does NOT grant any member (owner
included) automatic access to another member's private wellness data
(check-ins, CGM/nutrition, Google Health, Twin chat, PERSONAL goals,
habits, reports, simulations). Family Goals are a SEPARATE, deliberately
narrow system: shared coordination goals (e.g. "3 Spaziergänge diese
Woche") where a member explicitly submits their own participation/
progress for that shared goal — never a window into anyone's private
wellness data. `vt_wellness_goals` (personal goals) is untouched and
never read here. Shared challenges, family overview/dashboard, and any
automatic health-data sharing are explicitly OUT OF SCOPE and must not be
added here without a separate, deliberate task.

Reuses `core/auth.py::require_user`/`get_user_id_by_email` (no parallel
identity resolution), `core/plan_service.py::has_feature` (`family_profiles`/
`family_goals` features — Family tier only), and the same best-effort SMTP
pattern already used by `routers/contact.py` (no new email service).
"""

from __future__ import annotations

import os
import re
import smtplib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Literal

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
GOAL_TABLE = "vt_family_goals"
GOAL_MEMBER_TABLE = "vt_family_goal_members"

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


def _resolve_identities(user_ids: list[int]) -> dict[int, dict[str, object]]:
    """Shared by the member roster and Family Goal participants — never
    duplicated. Returns ONLY identity fields (email/display_name), never
    any wellness data."""
    if not user_ids:
        return {}
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

    return {
        uid: {"email": email, "display_name": display_name_by_email.get(email)}
        for uid, email in email_by_id.items()
    }


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

    identities = _resolve_identities([row["user_id"] for row in rows])
    members = []
    for row in rows:
        uid = int(row["user_id"])
        identity = identities.get(uid, {"email": "", "display_name": None})
        members.append({
            "user_id": uid,
            "email": identity["email"],
            "display_name": identity["display_name"],
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


# ---------------------------------------------------------------------------
# Family Goals V1 — shared coordination goals, NOT private wellness data.
# ---------------------------------------------------------------------------

TargetType = Literal["count", "days", "custom"]


class FamilyGoalCreate(BaseModel):
    title: str
    description: str | None = None
    target_type: TargetType = "count"
    target_value: float | None = None
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Bitte gib einen Titel für das Familienziel ein.")
        return stripped


class FamilyGoalUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    target_type: TargetType | None = None
    target_value: float | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: Literal["active", "archived"] | None = None


class FamilyGoalProgress(BaseModel):
    progress_value: float | None = None
    completed: bool | None = None


def _require_active_membership(user_id: int) -> dict[str, object]:
    """Family Goals require the caller to be a currently ACTIVE member
    (not merely invited) — a removed/left member loses access entirely,
    same as the membership roster itself."""
    membership = _get_open_membership(user_id)
    if not membership or membership.get("status") != "active":
        raise HTTPException(status_code=403, detail="Du bist aktuell kein aktives Family-Mitglied.")
    return membership


def _get_family_goal(goal_id: int, family_id: int) -> dict[str, object] | None:
    """Scoped by family_id so Family A can never reach Family B's goal by
    id — a 404 (not 403) so a guessed id can't be distinguished from one
    that doesn't exist."""
    try:
        rows = (
            supabase.table(GOAL_TABLE).select("*").eq("id", goal_id).eq("family_id", family_id).limit(1).execute().data
            or []
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _list_goal_participants(goal_id: int) -> list[dict[str, object]]:
    """ONLY identity + explicitly-submitted progress for THIS goal — never
    any private wellness data (Constitution privacy rule)."""
    try:
        rows = (
            supabase.table(GOAL_MEMBER_TABLE).select("*").eq("family_goal_id", goal_id).order("joined_at").execute().data
            or []
        )
    except Exception:
        return []

    identities = _resolve_identities([row["user_id"] for row in rows])
    return [
        {
            "user_id": int(row["user_id"]),
            "email": identities.get(int(row["user_id"]), {"email": ""}).get("email"),
            "display_name": identities.get(int(row["user_id"]), {"display_name": None}).get("display_name"),
            "progress_value": row.get("progress_value"),
            "completed": row.get("completed"),
        }
        for row in rows
    ]


def _serialize_goal(goal: dict[str, object]) -> dict[str, object]:
    participants = _list_goal_participants(int(goal["id"]))
    creator = _resolve_identities([int(goal["created_by_user_id"])]).get(
        int(goal["created_by_user_id"]), {"email": "", "display_name": None}
    )
    return {
        "id": int(goal["id"]),
        "title": goal.get("title"),
        "description": goal.get("description"),
        "target_type": goal.get("target_type"),
        "target_value": goal.get("target_value"),
        "start_date": goal.get("start_date"),
        "end_date": goal.get("end_date"),
        "status": goal.get("status"),
        "created_by": creator,
        "participants": participants,
        "participant_count": len(participants),
        "completed_count": sum(1 for p in participants if p.get("completed")),
    }


@router.get("/goals")
async def list_family_goals(authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    membership = _require_active_membership(current.user_id)
    family_id = int(membership["family_id"])

    try:
        rows = (
            supabase.table(GOAL_TABLE)
            .select("*")
            .eq("family_id", family_id)
            .eq("status", "active")
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    return {"goals": [_serialize_goal(row) for row in rows]}


@router.post("/goals")
async def create_family_goal(data: FamilyGoalCreate, authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    if not has_feature(current.email, "family_goals"):
        raise HTTPException(
            status_code=403, detail="Familienziele sind ein Family-Tarif-Feature. Upgrade auf Family."
        )
    membership = _require_active_membership(current.user_id)
    if membership.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Nur der Family-Owner kann Familienziele erstellen.")

    payload = {
        "family_id": int(membership["family_id"]),
        "created_by_user_id": current.user_id,
        "title": data.title,
        "description": data.description,
        "target_type": data.target_type,
        "target_value": data.target_value,
        "start_date": data.start_date.isoformat() if data.start_date else None,
        "end_date": data.end_date.isoformat() if data.end_date else None,
    }
    try:
        response = supabase.table(GOAL_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Familienziel konnte nicht gespeichert werden.") from exc
    goal_row = response.data[0] if response.data else None
    if not goal_row:
        raise HTTPException(status_code=500, detail="Familienziel konnte nicht gespeichert werden.")

    return _serialize_goal(goal_row)


@router.patch("/goals/{goal_id}")
async def update_family_goal(goal_id: int, data: FamilyGoalUpdate, authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    membership = _require_active_membership(current.user_id)
    if membership.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Nur der Family-Owner kann Familienziele bearbeiten.")

    goal = _get_family_goal(goal_id, int(membership["family_id"]))
    if not goal:
        raise HTTPException(status_code=404, detail="Familienziel nicht gefunden.")

    payload = data.model_dump(exclude_none=True)
    if "start_date" in payload and payload["start_date"] is not None:
        payload["start_date"] = data.start_date.isoformat()
    if "end_date" in payload and payload["end_date"] is not None:
        payload["end_date"] = data.end_date.isoformat()
    if payload:
        payload["updated_at"] = _now_iso()
        try:
            supabase.table(GOAL_TABLE).update(payload).eq("id", goal_id).execute()
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Familienziel konnte nicht aktualisiert werden.") from exc

    updated = _get_family_goal(goal_id, int(membership["family_id"])) or goal
    return _serialize_goal(updated)


@router.delete("/goals/{goal_id}")
async def archive_family_goal(goal_id: int, authorization: str | None = Header(default=None)):
    """Soft delete (archives), same convention as personal goals
    (`profile.py::delete_goal`) — a Family Goal's history stays queryable."""
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    membership = _require_active_membership(current.user_id)
    if membership.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Nur der Family-Owner kann Familienziele archivieren.")

    goal = _get_family_goal(goal_id, int(membership["family_id"]))
    if not goal:
        raise HTTPException(status_code=404, detail="Familienziel nicht gefunden.")

    try:
        supabase.table(GOAL_TABLE).update({"status": "archived", "updated_at": _now_iso()}).eq("id", goal_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Familienziel konnte nicht archiviert werden.") from exc

    return {"archived": True}


@router.post("/goals/{goal_id}/join")
async def join_family_goal(goal_id: int, authorization: str | None = Header(default=None)):
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    membership = _require_active_membership(current.user_id)

    goal = _get_family_goal(goal_id, int(membership["family_id"]))
    if not goal or goal.get("status") != "active":
        raise HTTPException(status_code=404, detail="Familienziel nicht gefunden.")

    try:
        existing = (
            supabase.table(GOAL_MEMBER_TABLE)
            .select("id")
            .eq("family_goal_id", goal_id)
            .eq("user_id", current.user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        existing = []
    if existing:
        return _serialize_goal(goal)

    try:
        supabase.table(GOAL_MEMBER_TABLE).insert({
            "family_goal_id": goal_id,
            "user_id": current.user_id,
        }).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Teilnahme konnte nicht gespeichert werden.") from exc

    return _serialize_goal(goal)


@router.patch("/goals/{goal_id}/progress")
async def update_family_goal_progress(
    goal_id: int, data: FamilyGoalProgress, authorization: str | None = Header(default=None)
):
    """A member may only ever update their OWN participation row — never
    another member's (enforced by filtering the update on `user_id`, not
    just `family_goal_id`)."""
    current = require_user(authorization)
    if current.user_id is None:
        raise HTTPException(status_code=401, detail="Konto konnte nicht aufgelöst werden.")
    membership = _require_active_membership(current.user_id)

    goal = _get_family_goal(goal_id, int(membership["family_id"]))
    if not goal:
        raise HTTPException(status_code=404, detail="Familienziel nicht gefunden.")

    try:
        own_rows = (
            supabase.table(GOAL_MEMBER_TABLE)
            .select("id")
            .eq("family_goal_id", goal_id)
            .eq("user_id", current.user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        own_rows = []
    if not own_rows:
        raise HTTPException(status_code=404, detail="Du nimmst an diesem Familienziel noch nicht teil.")

    payload = data.model_dump(exclude_none=True)
    if payload:
        payload["updated_at"] = _now_iso()
        try:
            supabase.table(GOAL_MEMBER_TABLE).update(payload).eq("id", own_rows[0]["id"]).execute()
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Fortschritt konnte nicht gespeichert werden.") from exc

    return _serialize_goal(goal)

