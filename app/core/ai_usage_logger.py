"""Central AI Usage Logging (Founder OS internal foundation #1 + #2).

Every AI request across the whole backend — the user-facing Twin Chat
(`routers/chat.py`) and every Founder-OS "Ask ..." endpoint (Business Coach,
CEO Intelligence, Auto Documentation, Founder Autopilot, Automation Engine's
failure explainer, Affiliate Intelligence's AI review) — writes exactly ONE
row to `vt_ai_usage_events`, success or failure. This is the single source of
truth for "KI-Requests", "KI-Tokenverbrauch", "KI-Kosten" und "KI-Fehler"
across the whole Founder OS instead of each module tracking (or not
tracking) its own copy.

Cost calculation is INTENTIONALLY gated behind two optional environment
variables (`OPENAI_PROMPT_PRICE_PER_1K_USD` / `OPENAI_COMPLETION_PRICE_PER_1K_USD`)
instead of a hardcoded public-pricing table baked into the code: OpenAI
pricing changes over time and differs per contract/region, so silently
assuming a fixed number would risk showing a wrong cost as if it were exact
billing data ("keine Zahlen schätzen"). Token counts themselves ARE always
real (taken directly from the OpenAI API response's `usage` field) — only
the USD conversion requires the founder to explicitly configure a real price.
"""

from __future__ import annotations

import os

from .supabase import supabase

TABLE = "vt_ai_usage_events"


def _resolve_price_per_1k(env_var: str) -> float | None:
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _compute_cost(prompt_tokens: int | None, completion_tokens: int | None) -> tuple[float | None, str | None]:
    prompt_price = _resolve_price_per_1k("OPENAI_PROMPT_PRICE_PER_1K_USD")
    completion_price = _resolve_price_per_1k("OPENAI_COMPLETION_PRICE_PER_1K_USD")

    if prompt_price is None or completion_price is None:
        return None, (
            "Kein Preis pro Token hinterlegt — Umgebungsvariablen "
            "OPENAI_PROMPT_PRICE_PER_1K_USD und OPENAI_COMPLETION_PRICE_PER_1K_USD "
            "setzen, um echte Kosten aus den Token-Zahlen zu berechnen."
        )
    if prompt_tokens is None or completion_tokens is None:
        return None, "Keine Token-Zahlen in der API-Antwort enthalten."

    cost = (prompt_tokens / 1000.0) * prompt_price + (completion_tokens / 1000.0) * completion_price
    return round(cost, 6), None


def log_ai_usage(
    *,
    feature: str,
    status: str = "success",
    email: str | None = None,
    model: str | None = None,
    usage: dict[str, object] | None = None,
    error_type: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Writes one row to the central AI usage log. Never raises — logging
    must never break the caller's actual request/response flow."""
    usage = usage or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    cost_usd, cost_note = _compute_cost(
        prompt_tokens if isinstance(prompt_tokens, int) else None,
        completion_tokens if isinstance(completion_tokens, int) else None,
    )

    row = {
        "email": email,
        "feature": feature,
        "model": model,
        "status": status,
        "error_type": error_type,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "cost_note": cost_note,
        "latency_ms": latency_ms,
    }
    try:
        supabase.table(TABLE).insert(row).execute()
    except Exception:
        pass


def get_ai_usage_summary(*, days: int = 1) -> dict[str, object]:
    """Real aggregation over `vt_ai_usage_events` for the given window (used
    by the KI Control Center + dashboards). Returns honest `None`s (not
    fabricated zeros) when the table can't be reached at all — but a
    genuinely empty (reachable) table correctly reports `0`, not `None`,
    since "zero requests logged" is a real, known fact."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = (
            supabase.table(TABLE)
            .select("status,error_type,prompt_tokens,completion_tokens,total_tokens,cost_usd,cost_note,latency_ms")
            .gte("created_at", since)
            .execute()
            .data
            or []
        )
    except Exception:
        return {
            "requests": None,
            "errors": None,
            "total_tokens": None,
            "cost_usd": None,
            "cost_note": "Nicht verfügbar — vt_ai_usage_events nicht erreichbar oder Migration 022 noch nicht ausgeführt.",
            "avg_latency_ms": None,
        }

    requests = len(rows)
    errors = sum(1 for r in rows if r.get("status") == "error")
    token_rows = [r for r in rows if isinstance(r.get("total_tokens"), int)]
    total_tokens = sum(r["total_tokens"] for r in token_rows) if token_rows else (0 if requests == 0 else None)
    cost_rows = [r for r in rows if isinstance(r.get("cost_usd"), (int, float))]
    cost_usd = round(sum(r["cost_usd"] for r in cost_rows), 6) if cost_rows else None
    cost_note = None if cost_rows else (
        rows[0].get("cost_note") if rows else "Kein Preis pro Token hinterlegt (siehe OPENAI_*_PRICE_PER_1K_USD)."
    )
    latency_rows = [r for r in rows if isinstance(r.get("latency_ms"), int)]
    avg_latency_ms = round(sum(r["latency_ms"] for r in latency_rows) / len(latency_rows)) if latency_rows else None

    return {
        "requests": requests,
        "errors": errors,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "cost_note": cost_note,
        "avg_latency_ms": avg_latency_ms,
    }
