"""AI Founder Task Manager — API (VitalTwin Release F3, Founder Operating
System, Module 3).

Mounted at `/api/admin/founder` in `app/main.py` (own file for module
isolation, same prefix as `routers/founder.py` /
`routers/founder_briefing.py`). Endpoints:

- `GET /tasks` — runs the (synchronous, in-request) detection rules from
  `core/founder_task_detector.py`, then returns the current task list plus
  the CEO-view summary counts. This is the *only* place detection runs —
  there is no scheduler/queue/cron.
- `PATCH /tasks/{id}/status` — founder-driven status change (Neu → In
  Bearbeitung → Warten → Erledigt/Archiviert).
- `POST /tasks/{id}/remind` — "Später erinnern": sets `status=warten` and
  `remind_at` to +24h.
- `POST /tasks/{id}/ignore` — sets `status=archiviert`, `ignored=true`.
- `POST /tasks/{id}/apply-suggestion` — executes a suggested fix, but
  **only** for the one suggestion that has a real implementation
  (re-checking affiliate links via `core/affiliate_link_checker.py`).
  Every other task's `suggested_action_available` is `false`, and this
  endpoint refuses to "execute" anything for them (400, not a fake
  success) — per spec, "Ausführung erfolgt nur nach Freigabe des
  Gründers" AND only for things that can genuinely, safely be automated.
  No pricing changes, no publishing, no product approvals happen here —
  those remain explicitly out of scope (see module docstring in
  `core/founder_task_detector.py` and `docs/AI_FOUNDER_TASK_MANAGER.md`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..core.admin_rbac import require_admin_permission
from ..core.affiliate_link_checker import check_link
from ..core.founder_task_detector import run_detection
from ..core.supabase import supabase

router = APIRouter()

TASK_TABLE = "vt_founder_tasks"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"

ALLOWED_STATUSES = {"neu", "in_bearbeitung", "warten", "erledigt", "archiviert"}


class StatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_STATUSES))}")
        return value


def _summary(tasks: list[dict]) -> dict:
    today = date.today().isoformat()
    open_tasks = [t for t in tasks if t.get("status") in ("neu", "in_bearbeitung", "warten")]
    critical = [t for t in open_tasks if t.get("priority") == "kritisch"]
    done_today = [t for t in tasks if t.get("status") == "erledigt" and str(t.get("resolved_at") or "") >= today]
    auto_detected = [t for t in tasks if t.get("auto_detected") and t.get("status") not in ("archiviert",)]
    auto_resolved = [t for t in tasks if t.get("auto_resolved")]
    return {
        "open_tasks": len(open_tasks),
        "critical_tasks": len(critical),
        "done_today": len(done_today),
        "auto_detected": len(auto_detected),
        "auto_resolved": len(auto_resolved),
    }


@router.get("/tasks")
async def list_founder_tasks(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_tasks")
    run_detection()
    try:
        tasks = supabase.table(TASK_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        tasks = []
    return {"items": tasks, "summary": _summary(tasks)}


@router.patch("/tasks/{task_id}/status")
async def update_task_status(task_id: str, data: StatusInput, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "manage_founder_tasks")
    payload = {"status": data.status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if data.status == "erledigt":
        payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table(TASK_TABLE).update(payload).eq("id", task_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Status konnte nicht geändert werden.") from exc
    return {"message": "Status aktualisiert."}


@router.post("/tasks/{task_id}/remind")
async def remind_later(task_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "manage_founder_tasks")
    remind_at = datetime.now(timezone.utc) + timedelta(hours=24)
    try:
        supabase.table(TASK_TABLE).update(
            {"status": "warten", "remind_at": remind_at.isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", task_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Erinnerung konnte nicht gesetzt werden.") from exc
    return {"message": "Erinnerung gesetzt.", "remind_at": remind_at.isoformat()}


@router.post("/tasks/{task_id}/ignore")
async def ignore_task(task_id: str, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "manage_founder_tasks")
    try:
        supabase.table(TASK_TABLE).update(
            {"status": "archiviert", "ignored": True, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", task_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Aufgabe konnte nicht ignoriert werden.") from exc
    return {"message": "Aufgabe ignoriert."}


@router.post("/tasks/{task_id}/apply-suggestion")
async def apply_suggestion(task_id: str, authorization: str | None = Header(default=None)):
    """The founder's click on this button IS the required approval
    ("Ausführung erfolgt nur nach Freigabe des Gründers") — but only for
    the single suggestion that is genuinely implemented: re-checking
    affiliate links. Any other task is refused with 400, never faked."""
    require_admin_permission(authorization, "manage_founder_tasks")
    try:
        rows = supabase.table(TASK_TABLE).select("*").eq("id", task_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden.")
    task = rows[0]

    if task.get("dedupe_key") != "affiliate_broken_links" or not task.get("suggested_action_available"):
        raise HTTPException(status_code=400, detail="Für diese Aufgabe ist keine automatische Ausführung verfügbar.")

    try:
        broken_products = (
            supabase.table(AFFILIATE_PRODUCT_TABLE)
            .select("id,affiliate_url")
            .eq("link_status", "broken")
            .execute()
            .data
            or []
        )
    except Exception:
        broken_products = []

    fixed = 0
    still_broken = 0
    for product in broken_products:
        result = check_link(product["affiliate_url"])
        try:
            supabase.table(AFFILIATE_PRODUCT_TABLE).update(
                {
                    "link_status": result["link_status"],
                    "link_http_status": result["http_status"],
                    "link_last_checked_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", product["id"]).execute()
        except Exception:
            continue
        if result["link_status"] == "ok":
            fixed += 1
        else:
            still_broken += 1

    # Re-run detection so the task auto-resolves if every link is fixed now.
    run_detection()

    return {"message": "Links erneut geprüft.", "fixed": fixed, "still_broken": still_broken}
