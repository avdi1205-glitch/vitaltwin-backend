"""Twin Context Engine: a minimal, question-scoped context for the AI.

Twin Intelligence Core — Etappe 7 §1.

Pure function over already-fetched, already-user-scoped data (every query
happens in the caller, e.g. `routers/chat.py`, always filtered by the
requesting user's own `email` — this module never touches the database and
therefore can never leak another user's data by construction). Builds a
compact, prioritized, size-capped natural-language context instead of ever
handing the AI provider a raw database dump.

Rules enforced here (Etappe 7 §1):

- **Keine gelöschten/verworfenen Memories**: only memories whose `status` is
  in `twin_memory.USABLE_STATUSES` ("active"/"confirmed") are considered —
  matches the Memory Loop's own definition of "usable for recommendations"
  (Etappe 5), reused here instead of re-defining a second notion of "active".
- **Sensible Freitexte nur bei echter Notwendigkeit**: raw free-text fields
  (check-in notes, reflection texts) are never included — only structured,
  aggregated, or already-reviewed-and-phrased values (trend averages,
  pattern summaries, memory `human_readable_value`, recommendation titles).
- **Kontextgröße begrenzen**: blocks are added in priority order (profile →
  goals → habits → check-ins → trends → memories → recommendations →
  feedback → data quality → patterns → daily plan) until the plan's
  character budget (`core/plans.py::get_context_char_limit`) is used up;
  remaining lower-priority blocks are silently dropped and `truncated=True`
  is reported — never a fabricated/shortened-but-mislabeled context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .twin_memory import USABLE_STATUSES

MAX_GOALS_IN_CONTEXT = 5
MAX_HABITS_IN_CONTEXT = 6
MAX_MEMORIES_IN_CONTEXT = 5
MAX_RECOMMENDATIONS_IN_CONTEXT = 3
MAX_PATTERNS_IN_CONTEXT = 3
MAX_PLAN_ACTIONS_IN_CONTEXT = 3

TREND_LABELS = {
    "sleep_hours": "Schlafdauer",
    "energy": "Energie",
    "movement_minutes": "Bewegung",
    "stress": "Stress",
    "mood": "Stimmung",
}


@dataclass(frozen=True)
class ContextSource:
    """One traceable "Datengrundlage" item — surfaced to the frontend so a
    reply can be marked transparently (Etappe 7 §6), never left unlabeled."""

    type: str  # "user_reported" | "trend" | "confirmed_memory" | "pattern" | "general_wellness_info"
    label: str


@dataclass(frozen=True)
class TwinContext:
    text: str
    sources: list[ContextSource] = field(default_factory=list)
    data_quality_note: str = ""
    truncated: bool = False


def _profile_block(profile: dict[str, object] | None) -> tuple[str, ContextSource] | None:
    if not profile:
        return None
    goals = profile.get("wellness_goals") or []
    if not goals:
        return None
    text = f"Wellness-Ziele aus dem Profil: {', '.join(str(g) for g in goals[:MAX_GOALS_IN_CONTEXT])}."
    return text, ContextSource(type="user_reported", label="Angaben aus deinem Profil")


def _goals_block(goals: list[dict[str, object]]) -> tuple[str, ContextSource] | None:
    active = [g for g in goals if g.get("status") == "active"][:MAX_GOALS_IN_CONTEXT]
    if not active:
        return None
    titles = ", ".join(f'"{g.get("title") or g.get("goal_type")}"' for g in active)
    return f"Aktive Ziele: {titles}.", ContextSource(type="user_reported", label="Deine aktiven Ziele")


def _habits_block(habits: list[dict[str, object]]) -> tuple[str, ContextSource] | None:
    active = [h for h in habits if h.get("status") == "active"][:MAX_HABITS_IN_CONTEXT]
    if not active:
        return None
    parts = []
    for h in active:
        rate = h.get("completion_rate_7d")
        rate_note = f" ({round(float(rate) * 100)}% diese Woche)" if isinstance(rate, (int, float)) else ""
        parts.append(f'"{h.get("name")}"{rate_note}')
    return f"Aktive Gewohnheiten: {', '.join(parts)}.", ContextSource(
        type="user_reported", label="Deine Gewohnheiten und Erfüllungsquote"
    )


def _trends_block(trends: dict[str, dict[str, object]]) -> tuple[str, ContextSource] | None:
    notes = []
    for field_name, label in TREND_LABELS.items():
        result = trends.get(field_name)
        if not result or result.get("average") is None:
            continue
        notes.append(f"{label}: Ø {result['average']} ({result.get('data_quality')})")
    if not notes:
        return None
    return f"Letzte Trends: {'; '.join(notes)}.", ContextSource(type="trend", label="Deine berechneten Trends")


def _memories_block(memories: list[dict[str, object]]) -> tuple[str, ContextSource] | None:
    usable = [m for m in memories if m.get("status") in USABLE_STATUSES][:MAX_MEMORIES_IN_CONTEXT]
    if not usable:
        return None
    notes = [str(m.get("human_readable_value")) for m in usable if m.get("human_readable_value")]
    if not notes:
        return None
    return f"Was der Twin bereits über dich gelernt hat: {' '.join(notes)}", ContextSource(
        type="confirmed_memory", label="Bestätigte Memories"
    )


def _recommendations_block(recommendations: list[dict[str, object]]) -> tuple[str, ContextSource] | None:
    proposed = [r for r in recommendations if r.get("status") == "proposed"][:MAX_RECOMMENDATIONS_IN_CONTEXT]
    if not proposed:
        return None
    titles = ", ".join(f'"{r.get("title")}"' for r in proposed)
    return f"Aktuell offene Empfehlungen: {titles}.", ContextSource(
        type="user_reported", label="Deine offenen Empfehlungen"
    )


def _feedback_block(feedback_summary: dict[str, int]) -> tuple[str, ContextSource] | None:
    if not feedback_summary:
        return None
    deprioritized = [cat for cat, penalty in feedback_summary.items() if penalty >= 2]
    if not deprioritized:
        return None
    return (
        f"Empfehlungen zu {', '.join(deprioritized)} wurden zuletzt öfter abgelehnt.",
        ContextSource(type="user_reported", label="Deine bisherige Rückmeldung zu Empfehlungen"),
    )


def _patterns_block(patterns: list[dict[str, object]]) -> tuple[str, ContextSource] | None:
    active = [p for p in patterns if p.get("status") == "active" and not p.get("contradicting")][
        :MAX_PATTERNS_IN_CONTEXT
    ]
    if not active:
        return None
    summaries = " ".join(str(p.get("summary")) for p in active if p.get("summary"))
    if not summaries:
        return None
    return summaries, ContextSource(type="pattern", label="Mögliche Muster in deinen Daten")


def _daily_plan_block(plan_actions: list[dict[str, object]]) -> tuple[str, ContextSource] | None:
    if not plan_actions:
        return None
    descriptions = [
        str(a.get("user_adjusted_description") or a.get("description")) for a in plan_actions[:MAX_PLAN_ACTIONS_IN_CONTEXT]
    ]
    return f"Heutiger Tagesplan: {', '.join(descriptions)}.", ContextSource(
        type="user_reported", label="Dein heutiger Tagesplan"
    )


def _data_quality_note(daily_entry_count: int) -> str:
    if daily_entry_count == 0:
        return "Noch keine Check-in-Daten vorhanden."
    if daily_entry_count < 3:
        return f"Nur {daily_entry_count} Check-in(s) vorhanden — Datenbasis ist noch dünn."
    return f"{daily_entry_count} Check-ins in den letzten Tagen vorhanden."


def build_twin_context(
    *,
    profile: dict[str, object] | None,
    goals: list[dict[str, object]],
    habits: list[dict[str, object]],
    daily_entry_count: int,
    trends: dict[str, dict[str, object]],
    confirmed_memories: list[dict[str, object]],
    active_recommendations: list[dict[str, object]],
    feedback_summary: dict[str, int],
    confirmed_patterns: list[dict[str, object]],
    daily_plan_actions: list[dict[str, object]],
    max_chars: int,
) -> TwinContext:
    quality_note = _data_quality_note(daily_entry_count)

    # Priority order per Etappe 7 §1's own listing.
    blocks = [
        _profile_block(profile),
        _goals_block(goals),
        _habits_block(habits),
        _trends_block(trends),
        _memories_block(confirmed_memories),
        _recommendations_block(active_recommendations),
        _feedback_block(feedback_summary),
        _patterns_block(confirmed_patterns),
        _daily_plan_block(daily_plan_actions),
    ]
    blocks = [b for b in blocks if b is not None]

    included_text: list[str] = [quality_note]
    included_sources: list[ContextSource] = []
    truncated = False
    budget = max_chars - len(quality_note)

    for text, source in blocks:
        if len(text) + 1 > budget:
            truncated = True
            continue
        included_text.append(text)
        included_sources.append(source)
        budget -= len(text) + 1

    return TwinContext(
        text=" ".join(included_text),
        sources=included_sources,
        data_quality_note=quality_note,
        truncated=truncated,
    )
