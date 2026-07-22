"""Twin-Conversation endpoints ("Frag deinen Twin").

Twin Intelligence Core — Etappe 7.

Wires together the Twin Context Engine (`services/twin_context.py`), the
AI Provider abstraction (`services/ai_provider.py`), and the conversation
safety rules (`services/twin_conversation.py`) behind the two endpoints the
existing frontend already calls (`GET /status`, `POST /ask` — see Etappe 7
§8: "Verbinde den bestehenden Bereich", no new endpoints needed).

Request flow for `POST /ask`, in order:

1. Authenticate (`core/auth.py`).
2. IP-based rate limit (`core/rate_limit.py`) — defense in depth alongside
   the per-user daily quota below.
3. Per-user daily quota + minimum spacing between requests (existing,
   plan-based, `core/plans.py`).
4. Deterministic prompt-injection gate (`twin_conversation.py`) — never
   reaches the AI provider, never costs anything beyond the quota tick.
5. Deterministic medical red-flag gate — same treatment.
6. Build a minimal, size-capped, source-labeled context
   (`services/twin_context.py`) from only this user's own data.
7. Call the AI provider through the `AIProvider` abstraction — never a
   fabricated reply on failure/timeout/invalid schema (§2).
8. Output-side medical-safety re-check on the model's reply.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..core.auth import require_email as _require_email_dependency
from ..core.plans import get_chat_daily_limit, get_context_char_limit
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services import personalization
from ..services.ai_provider import (
    MAX_INPUT_LENGTH,
    AIProvider,
    AIProviderError,
    AIProviderTimeoutError,
    AIRateLimitError,
    AIResponseValidationError,
    OpenAIProvider,
)
from ..services.habit_service import compute_habit_stats
from ..services.trends import compute_trend
from ..services.twin_context import build_twin_context
from ..services.twin_conversation import (
    MEDICAL_SAFETY_MESSAGE,
    PROMPT_INJECTION_REFUSAL_MESSAGE,
    build_conversation_system_prompt,
    contains_medical_red_flag,
    detect_prompt_injection,
)
from .users import is_premium_by_email

router = APIRouter()

USAGE_TABLE = "vt_chat_usage"
PROFILE_TABLE = "vt_user_profiles"
HABIT_TABLE = "vt_habits"
HABIT_ENTRY_TABLE = "vt_habit_entries"
GOAL_TABLE = "vt_wellness_goals"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
MEMORY_TABLE = "vt_twin_memory"
PATTERN_TABLE = "vt_twin_patterns"
RECOMMENDATION_TABLE = "vt_recommendations"
DAILY_PLAN_TABLE = "vt_daily_plans"
DAILY_PLAN_ACTION_TABLE = "vt_daily_plan_actions"

MIN_SECONDS_BETWEEN_REQUESTS = 3
IP_RATE_LIMIT_MAX_REQUESTS = 20
IP_RATE_LIMIT_WINDOW_SECONDS = 60

TREND_FIELDS = ("sleep_hours", "energy", "movement_minutes", "stress", "mood")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_INPUT_LENGTH)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Bitte gib eine Nachricht ein.")
        return stripped


def _require_email(authorization: str | None) -> str:
    return _require_email_dependency(authorization)


def _current_plan(email: str) -> str:
    # The database only distinguishes free/premium today (see Block 3 open
    # items) — pro/family accounts are treated as "premium" until a real
    # `plan` field exists.
    return "premium" if is_premium_by_email(email) else "free"


def _get_ai_provider() -> AIProvider:
    """Factory, not a module-level singleton — easy to monkeypatch in tests
    (`monkeypatch.setattr(chat_module, "_get_ai_provider", lambda: fake)`)."""
    return OpenAIProvider()


def _get_usage_row(email: str, today: str) -> dict[str, object] | None:
    try:
        response = (
            supabase.table(USAGE_TABLE)
            .select("*")
            .eq("email", email)
            .eq("usage_date", today)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception:
        return None


def _get_usage_today(email: str) -> tuple[int, dict[str, object] | None]:
    today = date.today().isoformat()
    row = _get_usage_row(email, today)
    return (int(row.get("count", 0)) if row else 0, row)


def _increment_usage(email: str, row: dict[str, object] | None) -> None:
    today = date.today().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        if row:
            supabase.table(USAGE_TABLE).update(
                {"count": int(row.get("count", 0)) + 1, "last_request_at": now_iso}
            ).eq("email", email).eq("usage_date", today).execute()
        else:
            supabase.table(USAGE_TABLE).insert(
                {"email": email, "usage_date": today, "count": 1, "last_request_at": now_iso}
            ).execute()
    except Exception:
        # Best effort: if the usage table isn't migrated yet, we still allow
        # the request rather than hard-failing the whole feature, but rate
        # limiting effectively degrades to "off" until the migration runs.
        pass


def _load_habits_with_stats(email: str, today: date) -> list[dict[str, object]]:
    try:
        habits_raw = supabase.table(HABIT_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        return []
    try:
        habit_entries = (
            supabase.table(HABIT_ENTRY_TABLE)
            .select("habit_id,entry_date,completed")
            .eq("email", email)
            .execute()
            .data
            or []
        )
    except Exception:
        habit_entries = []

    entries_by_habit: dict[str, list[dict[str, object]]] = {}
    for entry in habit_entries:
        entries_by_habit.setdefault(str(entry.get("habit_id")), []).append(entry)

    habits = []
    for habit in habits_raw:
        habit_id = str(habit.get("id"))
        stats = compute_habit_stats(
            entries_by_habit.get(habit_id, []), habit_created_at=habit.get("created_at"), today=today
        )
        habits.append({**habit, **stats})
    return habits


def _build_context_for_user(email: str, plan: str) -> tuple[str, list[dict[str, str]], bool]:
    """Gathers every raw data piece for this user only (every query scoped
    by `email`), then hands it to the pure `build_twin_context` to shape,
    redact, and cap it. Returns (context_text, sources, truncated)."""
    today = date.today()

    try:
        profile_resp = supabase.table(PROFILE_TABLE).select("*").eq("email", email).limit(1).execute()
        profile = profile_resp.data[0] if profile_resp.data else None
    except Exception:
        profile = None

    try:
        goals = (
            supabase.table(GOAL_TABLE)
            .select("*")
            .eq("email", email)
            .eq("status", "active")
            .is_("deleted_at", "null")
            .execute()
            .data
            or []
        )
    except Exception:
        goals = []

    habits = _load_habits_with_stats(email, today)

    try:
        daily_entries = (
            supabase.table(DAILY_ENTRY_TABLE)
            .select("*")
            .eq("email", email)
            .order("entry_date", desc=True)
            .limit(30)
            .execute()
            .data
            or []
        )
    except Exception:
        daily_entries = []

    trends: dict[str, dict[str, object]] = {}
    for field_name in TREND_FIELDS:
        result = compute_trend(daily_entries, field=field_name, window_days=7, today=today)
        trends[field_name] = {"average": result.average, "data_quality": result.data_quality}

    try:
        confirmed_memories = (
            supabase.table(MEMORY_TABLE)
            .select("*")
            .eq("email", email)
            .in_("status", ["active", "confirmed"])
            .is_("deleted_at", "null")
            .execute()
            .data
            or []
        )
    except Exception:
        confirmed_memories = []

    try:
        active_recommendations = (
            supabase.table(RECOMMENDATION_TABLE)
            .select("*")
            .eq("email", email)
            .eq("status", "proposed")
            .execute()
            .data
            or []
        )
    except Exception:
        active_recommendations = []

    try:
        recommendation_history = (
            supabase.table(RECOMMENDATION_TABLE)
            .select("category,status")
            .eq("email", email)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception:
        recommendation_history = []
    feedback_summary = personalization.compute_category_penalty(recommendation_history)

    try:
        confirmed_patterns = (
            supabase.table(PATTERN_TABLE)
            .select("*")
            .eq("email", email)
            .eq("status", "active")
            .eq("contradicting", False)
            .execute()
            .data
            or []
        )
    except Exception:
        confirmed_patterns = []

    daily_plan_actions: list[dict[str, object]] = []
    try:
        today_plan = (
            supabase.table(DAILY_PLAN_TABLE)
            .select("id")
            .eq("email", email)
            .eq("local_date", today.isoformat())
            .limit(1)
            .execute()
            .data
        )
        if today_plan:
            daily_plan_actions = (
                supabase.table(DAILY_PLAN_ACTION_TABLE)
                .select("description,user_adjusted_description")
                .eq("daily_plan_id", today_plan[0]["id"])
                .execute()
                .data
                or []
            )
    except Exception:
        daily_plan_actions = []

    context = build_twin_context(
        profile=profile,
        goals=goals,
        habits=habits,
        daily_entry_count=len(daily_entries),
        trends=trends,
        confirmed_memories=confirmed_memories,
        active_recommendations=active_recommendations,
        feedback_summary=feedback_summary,
        confirmed_patterns=confirmed_patterns,
        daily_plan_actions=daily_plan_actions,
        max_chars=get_context_char_limit(plan),
    )
    sources = [{"type": s.type, "label": s.label} for s in context.sources]
    return context.text, sources, context.truncated


@router.get("/status")
async def chat_status(authorization: str | None = Header(default=None)):
    email = _require_email(authorization)
    plan = _current_plan(email)
    limit = get_chat_daily_limit(plan)
    used, _ = _get_usage_today(email)
    return {
        "daily_limit": limit,
        "used_today": used,
        "remaining_today": max(0, limit - used),
        "plan": plan,
        "context_char_limit": get_context_char_limit(plan),
    }


@router.post("/ask")
async def ask_twin(data: ChatRequest, request: Request, authorization: str | None = Header(default=None)):
    email = _require_email(authorization)

    # IP-based rate limit — defense in depth, independent of the per-user
    # daily quota below (Etappe 7 §2 "Rate Limiting").
    enforce_rate_limit(
        request, "chat_ask", max_requests=IP_RATE_LIMIT_MAX_REQUESTS, window_seconds=IP_RATE_LIMIT_WINDOW_SECONDS
    )

    plan = _current_plan(email)
    limit = get_chat_daily_limit(plan)
    used_today, usage_row = _get_usage_today(email)

    if usage_row and usage_row.get("last_request_at"):
        try:
            last = datetime.fromisoformat(str(usage_row["last_request_at"]).replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
                raise HTTPException(status_code=429, detail="Bitte warte kurz, bevor du die nächste Frage stellst.")
        except (ValueError, TypeError):
            pass

    if used_today >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Tageslimit erreicht ({limit} Anfragen/Tag für deinen Tarif). "
            "Schau gerne morgen wieder vorbei oder upgrade für ein höheres Limit.",
        )

    # Prompt-injection gate — deterministic, runs before any AI call and
    # before the medical gate (Etappe 7 §5). Never reaches the model.
    if detect_prompt_injection(data.message):
        _increment_usage(email, usage_row)
        print("[chat] prompt-injection-gate triggered for user (content omitted)")
        return {
            "reply": PROMPT_INJECTION_REFUSAL_MESSAGE,
            "sources": [],
            "needs_more_data": False,
            "remaining_today": max(0, limit - used_today - 1),
            "safety_triggered": True,
            "context_truncated": False,
        }

    # Deterministic medical-safety gate (Etappe 4 origin, Etappe 7 §4).
    if contains_medical_red_flag(data.message):
        _increment_usage(email, usage_row)
        print("[chat] medical-safety-gate triggered for user (content omitted)")
        return {
            "reply": MEDICAL_SAFETY_MESSAGE,
            "sources": [],
            "needs_more_data": False,
            "remaining_today": max(0, limit - used_today - 1),
            "safety_triggered": True,
            "context_truncated": False,
        }

    try:
        profile_resp = supabase.table(PROFILE_TABLE).select("preferred_language").eq("email", email).limit(1).execute()
        language = (profile_resp.data[0].get("preferred_language") if profile_resp.data else None) or "de"
    except Exception:
        language = "de"

    context_text, sources, truncated = _build_context_for_user(email, plan)
    system_prompt = build_conversation_system_prompt(context_text=context_text, language=language)

    provider = _get_ai_provider()
    try:
        structured = await provider.generate_twin_response(system_prompt=system_prompt, user_message=data.message)
    except AIProviderTimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="Der Twin-Chat antwortet gerade zu langsam. Bitte versuche es erneut."
        ) from exc
    except AIRateLimitError as exc:
        raise HTTPException(
            status_code=503,
            detail="Der Twin-Chat ist gerade stark ausgelastet. Bitte versuche es gleich noch einmal.",
        ) from exc
    except AIResponseValidationError:
        # Etappe 7 §2/§5: never store or forward an unvalidated AI output —
        # fall back to a safe, honest message. The call still happened (and
        # therefore still costs), so usage is still incremented.
        _increment_usage(email, usage_row)
        return {
            "reply": "Deine Antwort konnte nicht sicher verarbeitet werden. Bitte versuche es erneut.",
            "sources": [],
            "needs_more_data": False,
            "remaining_today": max(0, limit - used_today - 1),
            "safety_triggered": False,
            "context_truncated": truncated,
        }
    except AIProviderError as exc:
        raise HTTPException(
            status_code=502, detail="Der Twin-Chat ist gerade nicht erreichbar. Bitte versuche es in Kürze erneut."
        ) from exc

    reply_text = structured.reply
    reply_sources = sources or [{"type": s.type, "label": s.label} for s in structured.sources]
    needs_more_data = structured.needs_more_data

    # Output-side safety net — even with a strict system prompt and
    # structured schema, never trust the model's reply text fully.
    if contains_medical_red_flag(reply_text):
        reply_text = MEDICAL_SAFETY_MESSAGE
        reply_sources = []
        needs_more_data = False

    _increment_usage(email, usage_row)
    print(f"[chat] request served, ts={int(time.time())}")

    return {
        "reply": reply_text,
        "sources": reply_sources,
        "needs_more_data": needs_more_data,
        "remaining_today": max(0, limit - used_today - 1),
        "safety_triggered": False,
        "context_truncated": truncated,
    }

