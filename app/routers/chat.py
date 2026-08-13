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
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..core.auth import require_email as _require_email_dependency
from ..core.auth import get_user_id_by_email
from ..core.ai_usage_logger import log_ai_usage
from ..core.concurrency import run_parallel
from ..core.plans import get_chat_daily_limit, get_context_char_limit
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services import personalization
from ..services import google_health_signals as ghs
from ..services import cgm_nutrition_signals as cns
from ..services import unified_twin_state as uts
from ..services import twin_longitudinal_comparison as tlc
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
from ..core.plan_service import get_effective_plan_by_email

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
HEALTH_ACTIVITY_TABLE = "health_activity_records"
HEALTH_SLEEP_TABLE = "health_sleep_records"
HEALTH_METRIC_TABLE = "health_metric_records"
CGM_TABLE = "vt_cgm_readings"
NUTRITION_TABLE = "vt_nutrition_entries"
BIOMARKER_CALC_TABLE = "vt_twin_calculations"
SNAPSHOT_TABLE = "vt_twin_context_snapshots"

MIN_SECONDS_BETWEEN_REQUESTS = 3
IP_RATE_LIMIT_MAX_REQUESTS = 20
IP_RATE_LIMIT_WINDOW_SECONDS = 60

TREND_FIELDS = ("sleep_hours", "energy", "movement_minutes", "stress", "mood")

# Twin Core Phase 1: how far back to load raw Google Health rows — must
# cover both the 7-day recent window and the (non-overlapping) 28-day
# baseline window ending 7 days ago, i.e. 35 days total.
GOOGLE_HEALTH_LOOKBACK_DAYS = ghs.RECENT_WINDOW_DAYS + ghs.BASELINE_WINDOW_DAYS

# Twin Core Phase 2: same 35-day lookback principle for CGM/Nutrition.
CGM_NUTRITION_LOOKBACK_DAYS = cns.RECENT_WINDOW_DAYS + cns.BASELINE_WINDOW_DAYS



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
    # Effective plan lookup (VitalTwin Plan System, Beta-aware) — Pro/Family
    # now correctly get their own configured
    # CHAT_DAILY_LIMITS/CONTEXT_CHAR_LIMITS instead of being collapsed into
    # "premium", and a Beta-granted tester gets the SAME real limits a
    # paying Pro/Family customer would.
    return get_effective_plan_by_email(email)


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


def _load_habits_raw(email: str) -> list[dict[str, object]]:
    try:
        return supabase.table(HABIT_TABLE).select("*").eq("email", email).execute().data or []
    except Exception:
        return []


def _load_habit_entries(email: str) -> list[dict[str, object]]:
    try:
        return (
            supabase.table(HABIT_ENTRY_TABLE)
            .select("habit_id,entry_date,completed")
            .eq("email", email)
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def _combine_habits_with_stats(
    habits_raw: list[dict[str, object]], habit_entries: list[dict[str, object]], today: date
) -> list[dict[str, object]]:
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


def _build_context_for_user(email: str, plan: str) -> tuple[str, list[dict[str, str]], bool, dict[str, object] | None]:
    """Gathers every raw data piece for this user only (every query scoped
    by `email`), then hands it to the pure `build_twin_context` to shape,
    redact, and cap it. Returns (context_text, sources, truncated, profile)
    — the caller reuses the already-fetched `profile` (e.g. for
    `preferred_language`) instead of issuing a second, separate profile
    query for the same user.

    All independent lookups below run concurrently via `run_parallel`
    (same pattern as the Founder-OS dashboards) instead of one after
    another — this used to be ~10-12 sequential Supabase round-trips on
    every single "Frag deinen Twin" request, the slowest user-facing
    endpoint in the codebase."""
    today = date.today()

    def _profile() -> dict[str, object] | None:
        try:
            resp = supabase.table(PROFILE_TABLE).select("*").eq("email", email).limit(1).execute()
            return resp.data[0] if resp.data else None
        except Exception:
            return None

    def _goals() -> list[dict[str, object]]:
        try:
            return (
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
            return []

    def _daily_entries() -> list[dict[str, object]]:
        try:
            return (
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
            return []

    def _confirmed_memories() -> list[dict[str, object]]:
        try:
            return (
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
            return []

    def _active_recommendations() -> list[dict[str, object]]:
        try:
            return (
                supabase.table(RECOMMENDATION_TABLE)
                .select("*")
                .eq("email", email)
                .eq("status", "proposed")
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _recommendation_history() -> list[dict[str, object]]:
        try:
            return (
                supabase.table(RECOMMENDATION_TABLE)
                .select("category,status")
                .eq("email", email)
                .limit(100)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _confirmed_patterns() -> list[dict[str, object]]:
        try:
            return (
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
            return []

    def _today_plan_id() -> str | None:
        try:
            rows = (
                supabase.table(DAILY_PLAN_TABLE)
                .select("id")
                .eq("email", email)
                .eq("local_date", today.isoformat())
                .limit(1)
                .execute()
                .data
            )
            return rows[0]["id"] if rows else None
        except Exception:
            return None

    def _user_id() -> int | None:
        # Twin Core Phase 1: Google Health tables are keyed by `user_id`,
        # not `email` — reuses the EXISTING core/auth.py resolver, no
        # second identity system.
        return get_user_id_by_email(email)

    cgm_nutrition_since_iso = (today - timedelta(days=CGM_NUTRITION_LOOKBACK_DAYS)).isoformat()

    def _cgm_rows() -> list[dict[str, object]]:
        # Twin Core Phase 2: `vt_cgm_readings` is `email`-keyed (unlike
        # Google Health's `user_id`-keyed tables) — scoped exactly like
        # every other query in this function.
        try:
            return (
                supabase.table(CGM_TABLE)
                .select("glucose_value,reading_at,source")
                .eq("email", email)
                .gte("reading_at", cgm_nutrition_since_iso)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _nutrition_rows() -> list[dict[str, object]]:
        try:
            return (
                supabase.table(NUTRITION_TABLE)
                .select("calories,protein,carbs,fat,logged_at")
                .eq("email", email)
                .gte("logged_at", cgm_nutrition_since_iso)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _biomarker_rows() -> list[dict[str, object]]:
        # Twin Core Phase 4: same `vt_twin_calculations` table/columns
        # `routers/twin.py::get_twin_history` and
        # `routers/profile.py::get_advanced_twin_overview` already read —
        # email-keyed like every other table in this function.
        try:
            return (
                supabase.table(BIOMARKER_CALC_TABLE)
                .select("created_at,biologisches_alter,differenz,scenarios,marker_breakdown")
                .eq("email", email)
                .order("created_at", desc=True)
                .limit(5)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    def _recent_snapshots() -> list[dict[str, object]]:
        # Twin Core Phase 7: the 2 most recent already-persisted Twin State
        # Snapshots -- never rebuilds a fresh Unified Twin State here, and
        # never sends the raw snapshot JSON to the LLM (only the small
        # deterministic comparison text built below).
        try:
            return (
                supabase.table(SNAPSHOT_TABLE)
                .select("snapshot,created_at")
                .eq("email", email)
                .order("created_at", desc=True)
                .limit(2)
                .execute()
                .data
                or []
            )
        except Exception:
            return []

    (
        profile,
        goals,
        habits_raw,
        habit_entries,
        daily_entries,
        confirmed_memories,
        active_recommendations,
        recommendation_history,
        confirmed_patterns,
        today_plan_id,
        user_id,
        cgm_rows,
        nutrition_rows,
        biomarker_rows,
        recent_snapshots,
    ) = run_parallel(
        _profile,
        _goals,
        lambda: _load_habits_raw(email),
        lambda: _load_habit_entries(email),
        _daily_entries,
        _confirmed_memories,
        _active_recommendations,
        _recommendation_history,
        _confirmed_patterns,
        _today_plan_id,
        _user_id,
        _cgm_rows,
        _nutrition_rows,
        _biomarker_rows,
        _recent_snapshots,
    )

    habits = _combine_habits_with_stats(habits_raw, habit_entries, today)

    trends: dict[str, dict[str, object]] = {}
    for field_name in TREND_FIELDS:
        result = compute_trend(daily_entries, field=field_name, window_days=7, today=today)
        trends[field_name] = {"average": result.average, "data_quality": result.data_quality}

    feedback_summary = personalization.compute_category_penalty(recommendation_history)

    google_health_context = _build_google_health_context(user_id, daily_entries, today)
    cgm_context = cns.cgm_to_context_dict(cns.build_cgm_signal(cgm_rows, today=today))
    nutrition_context = {
        signal: cns.nutrition_to_context_dict(cns.build_nutrition_signal(nutrition_rows, signal=signal, today=today))
        for signal in cns.NUTRITION_FIELD_CONFIG
    }
    biomarker_summary = uts.summarize_biomarker_state(biomarker_rows, today=today)
    biomarker_context = {"status": biomarker_summary.status, "values": biomarker_summary.values}

    twin_history_context: dict[str, object] = {"available": False}
    if len(recent_snapshots) >= 2:
        ordered = sorted(recent_snapshots, key=lambda row: str(row.get("created_at") or ""), reverse=True)
        newest, previous = ordered[0], ordered[1]
        comparison = tlc.compare_snapshots(
            previous.get("snapshot"), newest.get("snapshot") or {},
            older_created_at=previous.get("created_at"), newer_created_at=newest.get("created_at"),
        )
        twin_history_context = {"available": comparison.available, "explanations": comparison.explanations}

    daily_plan_actions: list[dict[str, object]] = []
    if today_plan_id:
        try:
            daily_plan_actions = (
                supabase.table(DAILY_PLAN_ACTION_TABLE)
                .select("description,user_adjusted_description")
                .eq("daily_plan_id", today_plan_id)
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
        google_health=google_health_context,
        cgm=cgm_context,
        nutrition=nutrition_context,
        biomarker=biomarker_context,
        twin_history=twin_history_context,
    )
    sources = [{"type": s.type, "label": s.label} for s in context.sources]
    return context.text, sources, context.truncated, profile


def _load_google_health_rows(
    user_id: int, table: str, *, data_type: str | None, since_iso: str, provider: str | None = None
) -> list[dict[str, object]]:
    # Google Health tables are `user_id`-keyed (never `email`) — an
    # explicit `.eq("user_id", user_id)` here is the ONLY thing standing
    # between one user's automatically-synced health data and another's
    # Twin context (Step 8: strict user isolation). `provider` defaults to
    # None (no filter) to keep any other existing caller byte-identical;
    # `_build_google_health_context` below always passes it explicitly now
    # so Google Health and Health Connect rows are never blended into one
    # unfiltered query (Health Connect Phase 2 — prevents double-counting a
    # day that has automatic steps from both sources).
    try:
        query = supabase.table(table).select("*").eq("user_id", user_id)
        if data_type:
            query = query.eq("data_type", data_type)
        if provider:
            query = query.eq("provider", provider)
        time_field = "observed_at" if table == HEALTH_METRIC_TABLE else "start_time"
        return query.gte(time_field, since_iso).execute().data or []
    except Exception:
        return []


def _build_google_health_context(
    user_id: int | None, daily_entries: list[dict[str, object]], today: date
) -> dict[str, dict[str, object]]:
    """Twin Core Phase 1: reads VitalTwin's OWN already-persisted Google
    Health records (never the Google API — provider independence,
    Constitution rule 8) and shapes them into the plain-dict form
    `twin_context.py` consumes. Returns `{}` (never raises, never invents
    data) if `user_id` couldn't be resolved or nothing is stored."""
    if user_id is None:
        return {}

    since_iso = (today - timedelta(days=GOOGLE_HEALTH_LOOKBACK_DAYS)).isoformat()

    def _rows(signal: str, provider: str) -> list[dict[str, object]]:
        config = ghs.SIGNAL_CONFIG[signal]
        return _load_google_health_rows(
            user_id, str(config["table"]), data_type=config.get("data_type"), since_iso=since_iso, provider=provider
        )

    signal_names = list(ghs.SIGNAL_CONFIG.keys())
    google_rows_by_signal = dict(
        zip(signal_names, run_parallel(*[lambda s=name: _rows(s, "google_health") for name in signal_names]))
    )
    # Only "steps" can currently come from Health Connect (Phase 2 —
    # READ_STEPS only); fetching it for every signal keeps this loop
    # uniform and future-proof at negligible cost (empty result for the rest).
    health_connect_rows_by_signal = dict(
        zip(signal_names, run_parallel(*[lambda s=name: _rows(s, "health_connect") for name in signal_names]))
    )

    result: dict[str, dict[str, object]] = {}
    for signal in signal_names:
        rows = google_rows_by_signal[signal]
        if signal in ghs.MANUAL_FIELD_FOR_SIGNAL:
            # Step 5: source precedence — real Google Health data wins when
            # present, Health Connect is the next automatic tier, manual
            # check-in data is the final fallback; neither history is ever
            # modified.
            resolved = ghs.resolve_trend_source(
                signal=signal,
                google_rows=rows,
                manual_entries=daily_entries,
                today=today,
                health_connect_rows=health_connect_rows_by_signal[signal],
            )
            result[signal] = ghs.signal_to_context_dict(resolved, unit=str(ghs.SIGNAL_CONFIG[signal]["unit"]))
        else:
            built = ghs.build_signal(rows, signal=signal, today=today)
            result[signal] = ghs.signal_to_context_dict(built)
    return result


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

    # Reuses the profile already loaded inside _build_context_for_user
    # (same row, same email) instead of issuing a second, separate
    # `PROFILE_TABLE` query just for `preferred_language`.
    context_text, sources, truncated, profile = _build_context_for_user(email, plan)
    language = (profile.get("preferred_language") if profile else None) or "de"
    system_prompt = build_conversation_system_prompt(context_text=context_text, language=language)

    provider = _get_ai_provider()
    start = time.perf_counter()
    try:
        structured = await provider.generate_twin_response(system_prompt=system_prompt, user_message=data.message)
    except AIProviderTimeoutError as exc:
        log_ai_usage(
            email=email, feature="twin_chat", status="error", error_type="AIProviderTimeoutError",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        raise HTTPException(
            status_code=504, detail="Der Twin-Chat antwortet gerade zu langsam. Bitte versuche es erneut."
        ) from exc
    except AIRateLimitError as exc:
        log_ai_usage(
            email=email, feature="twin_chat", status="error", error_type="AIRateLimitError",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        raise HTTPException(
            status_code=503,
            detail="Der Twin-Chat ist gerade stark ausgelastet. Bitte versuche es gleich noch einmal.",
        ) from exc
    except AIResponseValidationError:
        # Etappe 7 §2/§5: never store or forward an unvalidated AI output —
        # fall back to a safe, honest message. The call still happened (and
        # therefore still costs), so usage is still incremented.
        log_ai_usage(
            email=email, feature="twin_chat", status="error", error_type="AIResponseValidationError",
            model=getattr(provider, "last_model", None), usage=getattr(provider, "last_usage", None),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
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
        log_ai_usage(
            email=email, feature="twin_chat", status="error", error_type="AIProviderError",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        raise HTTPException(
            status_code=502, detail="Der Twin-Chat ist gerade nicht erreichbar. Bitte versuche es in Kürze erneut."
        ) from exc

    log_ai_usage(
        email=email, feature="twin_chat", status="success",
        model=getattr(provider, "last_model", None), usage=getattr(provider, "last_usage", None),
        latency_ms=int((time.perf_counter() - start) * 1000),
    )

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

