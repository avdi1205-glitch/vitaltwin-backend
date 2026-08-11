"""Twin Learning Timeline ("Was dein Twin über dich gelernt hat").

Twin Core Phase 5.

Pure read/composition layer over the ALREADY-PERSISTED
`vt_twin_learning_events` table (`core/learning_events.py`) — this module
writes NOTHING, invents NO new event type, and recalculates NOTHING. Today
that table is written from exactly 13 real (event_type, source_type)
call-site combinations across `routers/twin_memory.py`,
`routers/profile.py::update_goal`, and `routers/recommendations.py` — see
`_EVENT_TYPE_DOMAIN_MAP`/`_resolve_category_and_domain` below for the full
inventory. A raw database write is not automatically "learning" (Step 3):
only event types with a real, deliberate call site are ever mapped to a
customer-facing category — anything else (e.g. a stray legacy `payload`-only
row from before email-scoping existed, or any future event type added
without updating this module) is silently OMITTED from the customer
timeline rather than shown as a raw/confusing entry.

No "CONTRADICTED" category was emitted in Phase 5: no call site recorded a
pattern transitioning to `contradicting=True` on an already-existing row.
Twin Core Phase 6 Part A closes this gap — `routers/twin_memory.py`'s
pattern re-evaluation loop now records ONE `muster_widerspruch_erkannt`
event on the genuine "not contradicting -> contradicting" transition (never
repeated on unchanged re-reads), mapped here to CONTRADICTED.
"""

from __future__ import annotations

from dataclasses import dataclass

from .personalization import REASON_REPEAT_THRESHOLD

CATEGORY_LEARNED = "LEARNED"
CATEGORY_CONFIRMED = "CONFIRMED"
CATEGORY_UPDATED = "UPDATED"
CATEGORY_CORRECTED_BY_USER = "CORRECTED_BY_USER"
CATEGORY_DISCARDED = "DISCARDED"
CATEGORY_FEEDBACK_ADAPTATION = "FEEDBACK_ADAPTATION"
CATEGORY_CONTRADICTED = "CONTRADICTED"

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# Every (event_type -> (category, related_domain)) pair actually written
# today, except "muster_erkannt" which is ambiguous by itself (used for both
# a new twin_memory row AND a new twin_pattern row) and is resolved via
# source_type instead, see `_SOURCE_TYPE_DOMAIN`/`_resolve_category_and_domain`.
_EVENT_TYPE_MAP: dict[str, tuple[str, str]] = {
    "praeferenz_erkannt": (CATEGORY_LEARNED, "memory"),
    "memory_erstellt": (CATEGORY_LEARNED, "memory"),
    "praeferenz_bestaetigt": (CATEGORY_CONFIRMED, "memory"),
    "memory_bestaetigt": (CATEGORY_CONFIRMED, "memory"),
    "memory_korrigiert": (CATEGORY_CORRECTED_BY_USER, "memory"),
    "memory_abgelehnt": (CATEGORY_DISCARDED, "memory"),
    "memory_archiviert": (CATEGORY_DISCARDED, "memory"),
    "memory_geloescht": (CATEGORY_DISCARDED, "memory"),
    "muster_verworfen": (CATEGORY_DISCARDED, "pattern"),
    "muster_widerspruch_erkannt": (CATEGORY_CONTRADICTED, "pattern"),
    "ziel_angepasst": (CATEGORY_UPDATED, "goal"),
    "empfehlung_abgelehnt": (CATEGORY_FEEDBACK_ADAPTATION, "recommendation"),
    "empfehlung_erfolgreich": (CATEGORY_FEEDBACK_ADAPTATION, "recommendation"),
}

_SOURCE_TYPE_DOMAIN: dict[str, str] = {
    "twin_memory": "memory",
    "twin_pattern": "pattern",
    "wellness_goal": "goal",
    "recommendation_decision": "recommendation",
    "recommendation_outcome": "recommendation",
}

# Per-domain "still current" statuses, reused from the exact same status
# vocabularies `services/twin_memory.py`/`pattern_detection`/goals already
# use — never a second definition of "active"/"discarded".
_CURRENT_STATUSES_BY_DOMAIN: dict[str, frozenset[str]] = {
    "memory": frozenset({"candidate", "active", "confirmed"}),
    "pattern": frozenset({"active"}),
    "goal": frozenset({"active"}),
}


@dataclass(frozen=True)
class LearningTimelineEntry:
    id: str
    occurred_at: str | None
    category: str
    related_domain: str
    title: str
    summary: str
    confidence_before: float | None = None
    confidence_after: float | None = None
    current_status: str | None = None
    is_current: bool | None = None


def _resolve_category_and_domain(event_type: str | None, source_type: str | None) -> tuple[str, str] | None:
    if event_type == "muster_erkannt":
        domain = _SOURCE_TYPE_DOMAIN.get(source_type or "")
        return (CATEGORY_LEARNED, domain) if domain else None
    return _EVENT_TYPE_MAP.get(event_type or "")


def _confidence_clause(previous_state: dict[str, object], new_state: dict[str, object]) -> str:
    """Twin Core Phase 6 Part C: only ever shows a percentage when the
    stored values genuinely support it — never fabricates one side."""
    before = previous_state.get("confidence")
    after = new_state.get("confidence")
    before_ok = isinstance(before, (int, float))
    after_ok = isinstance(after, (int, float))
    if before_ok and after_ok:
        return f" Vertrauen: {round(before * 100)}% \u2192 {round(after * 100)}%."
    if after_ok:
        return f" Vertrauen: {round(after * 100)}%."
    return ""


_REASON_CATEGORY_SENTENCES: dict[str, str] = {
    "timing_not_suitable": "dass diese Art Empfehlung für dich wiederholt zeitlich unpassend war",
    "too_difficult": "dass diese Art Empfehlung für dich wiederholt zu anspruchsvoll war",
    "not_relevant": "dass diese Art Empfehlung für dich wiederholt nicht relevant war",
    "already_doing": "dass du das bereits regelmäßig umsetzt",
    "preference_conflict": "dass diese Art Empfehlung nicht zu deinen Vorlieben passt",
}


def _build_title_and_summary(
    event_type: str,
    previous_state: dict[str, object],
    new_state: dict[str, object],
    reason: str | None,
    *,
    reason_repeat_counts: dict[tuple[str, str], int] | None = None,
) -> tuple[str, str]:
    if event_type == "praeferenz_erkannt":
        return "Neue Präferenz erkannt", (
            f"Dein Twin hat erkannt: {reason}." if reason else "Dein Twin hat eine neue Präferenz erkannt."
        )
    if event_type == "muster_erkannt":
        return "Neues Muster erkannt", (
            f"Dein Twin hat ein neues Muster erkannt: {reason}." if reason else "Dein Twin hat ein neues Muster erkannt."
        )
    if event_type == "memory_erstellt":
        if new_state.get("status") == "confirmed":
            return "Von dir mitgeteilt", (
                f"Du hast deinem Twin mitgeteilt: {reason}." if reason else "Du hast deinem Twin etwas direkt mitgeteilt."
            )
        return "Neue Beobachtung gespeichert", (
            f"Dein Twin hat aus deinen Daten gespeichert: {reason}." if reason
            else "Dein Twin hat aus deinen Daten eine neue Beobachtung gespeichert."
        )
    if event_type == "praeferenz_bestaetigt":
        summary = "Nach mehreren Beobachtungen hat dein Twin diese Präferenz als bestätigt eingestuft."
        return "Präferenz bestätigt", summary + _confidence_clause(previous_state, new_state)
    if event_type == "memory_bestaetigt":
        summary = f"Du hast bestätigt: {reason}." if reason else "Du hast diese Beobachtung deines Twins bestätigt."
        return "Von dir bestätigt", summary + _confidence_clause(previous_state, new_state)
    if event_type == "memory_korrigiert":
        corrected = new_state.get("human_readable_value")
        summary = f'Du hast dies korrigiert zu: "{corrected}".' if corrected else "Du hast diese Beobachtung korrigiert."
        return "Von dir korrigiert", summary + _confidence_clause(previous_state, new_state)
    if event_type == "memory_abgelehnt":
        summary = f"Du hast diese Beobachtung abgelehnt: {reason}." if reason else "Du hast diese Beobachtung abgelehnt."
        return "Von dir abgelehnt", summary + _confidence_clause(previous_state, new_state)
    if event_type == "memory_archiviert":
        return "Archiviert", (
            f"Du hast diese Beobachtung archiviert: {reason}." if reason else "Du hast diese Beobachtung archiviert."
        )
    if event_type == "memory_geloescht":
        return "Gelöscht", "Du hast diese Beobachtung gelöscht."
    if event_type == "muster_verworfen":
        return "Muster verworfen", (
            f"Du hast dieses Muster verworfen: {reason}." if reason else "Du hast dieses Muster verworfen."
        )
    if event_type == "muster_widerspruch_erkannt":
        # Twin Core Phase 6 Part A — deterministic, non-causal wording per
        # the task's own required example; never "this was wrong"/"proves".
        return "Muster infrage gestellt", "Neuere Daten unterstützen ein früher erkanntes Muster derzeit nicht mehr."
    if event_type == "ziel_angepasst":
        changed_fields = [key for key in ("status", "target_value", "target_date", "title") if key in new_state]
        if "status" in changed_fields:
            return "Ziel aktualisiert", f"Der Status deines Ziels wurde zu \"{new_state.get('status')}\" geändert."
        if changed_fields:
            return "Ziel aktualisiert", "Dein Ziel wurde angepasst."
        return "Ziel aktualisiert", "Dein Ziel wurde angepasst."
    if event_type == "empfehlung_abgelehnt":
        summary = (
            f"Du hast eine Empfehlung abgelehnt: {reason}. Dein Twin berücksichtigt dieses Feedback bei zukünftigen Empfehlungen."
            if reason
            else "Du hast eine Empfehlung abgelehnt. Dein Twin berücksichtigt dieses Feedback bei zukünftigen Empfehlungen."
        )
        # Twin Core Phase 6 Part B — only after genuinely REPEATED evidence
        # (never from this single event alone) does the timeline name the
        # specific reason pattern the Twin is now accounting for.
        category = new_state.get("category")
        reason_category = new_state.get("reason_category")
        if reason_repeat_counts and category and reason_category:
            count = reason_repeat_counts.get((str(category), str(reason_category)), 0)
            sentence = _REASON_CATEGORY_SENTENCES.get(str(reason_category))
            if sentence and count >= REASON_REPEAT_THRESHOLD:
                summary = f"Dein Twin berücksichtigt, {sentence}."
        return "Empfehlung abgelehnt", summary
    if event_type == "empfehlung_erfolgreich":
        return "Empfehlung erfolgreich umgesetzt", (
            f"Du hast eine Empfehlung erfolgreich umgesetzt: {reason}." if reason else "Du hast eine Empfehlung erfolgreich umgesetzt."
        )
    return event_type, ""


def summarize_learning_event(
    row: dict[str, object], *, reason_repeat_counts: dict[tuple[str, str], int] | None = None
) -> LearningTimelineEntry | None:
    """Maps one raw `vt_twin_learning_events` row into a customer-safe
    timeline entry, or None if this row's event_type is not a recognized,
    customer-worthy learning moment (Step 9: filter noise)."""
    event_type = row.get("event_type")
    source_type = row.get("source_type")
    resolved = _resolve_category_and_domain(
        event_type if isinstance(event_type, str) else None,
        source_type if isinstance(source_type, str) else None,
    )
    if resolved is None:
        return None
    category, domain = resolved

    previous_state = row.get("previous_state")
    previous_state = previous_state if isinstance(previous_state, dict) else {}
    new_state = row.get("new_state")
    new_state = new_state if isinstance(new_state, dict) else {}
    reason = row.get("reason")
    reason = reason if isinstance(reason, str) and reason.strip() else None

    title, summary = _build_title_and_summary(
        str(event_type), previous_state, new_state, reason, reason_repeat_counts=reason_repeat_counts
    )

    confidence_before = previous_state.get("confidence")
    confidence_after = new_state.get("confidence")

    return LearningTimelineEntry(
        id=str(row.get("id")),
        occurred_at=row.get("created_at"),
        category=category,
        related_domain=domain,
        title=title,
        summary=summary,
        confidence_before=confidence_before if isinstance(confidence_before, (int, float)) else None,
        confidence_after=confidence_after if isinstance(confidence_after, (int, float)) else None,
    )


def build_learning_timeline(
    rows: list[dict[str, object]], *, source_ids_by_domain: dict[str, dict[str, dict[str, object]]] | None = None
) -> list[LearningTimelineEntry]:
    """Composes a deterministic, newest-first, customer-safe timeline from
    already-fetched raw event rows (the caller does the DB fetch/pagination —
    same "pure function over already-fetched data" convention as every other
    Twin Core service).

    `source_ids_by_domain` optionally enriches each entry with the CURRENT
    state of its related entity (Step 5) — e.g.
    `{"memory": {"<id>": <current memory row>}, ...}` — read-only, never
    rewrites the historical previous_state/new_state already captured at
    event time."""
    sorted_rows = sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)

    # Twin Core Phase 6 Part B: count how often the SAME (category,
    # reason_category) pair recurs among the rows in THIS batch — repeated
    # evidence, never a single rejection, drives the timeline's stronger
    # wording (see `_build_title_and_summary`'s "empfehlung_abgelehnt" branch).
    reason_repeat_counts: dict[tuple[str, str], int] = {}
    for row in sorted_rows:
        if row.get("event_type") != "empfehlung_abgelehnt":
            continue
        new_state = row.get("new_state")
        new_state = new_state if isinstance(new_state, dict) else {}
        category = new_state.get("category")
        reason_category = new_state.get("reason_category")
        if category and reason_category and reason_category != "other":
            key = (str(category), str(reason_category))
            reason_repeat_counts[key] = reason_repeat_counts.get(key, 0) + 1

    entries: list[LearningTimelineEntry] = []
    for row in sorted_rows:
        entry = summarize_learning_event(row, reason_repeat_counts=reason_repeat_counts)
        if entry is None:
            continue
        entry = _enrich_with_current_state(entry, row, source_ids_by_domain or {})
        entries.append(entry)
    return entries


def _enrich_with_current_state(
    entry: LearningTimelineEntry, row: dict[str, object], source_ids_by_domain: dict[str, dict[str, dict[str, object]]]
) -> LearningTimelineEntry:
    current_rows_for_domain = source_ids_by_domain.get(entry.related_domain)
    if not current_rows_for_domain:
        return entry
    source_id = row.get("source_id")
    current_row = current_rows_for_domain.get(str(source_id)) if source_id else None
    if current_row is None:
        return entry
    current_status = current_row.get("status")
    current_status = current_status if isinstance(current_status, str) else None
    allowed = _CURRENT_STATUSES_BY_DOMAIN.get(entry.related_domain, frozenset())
    is_current = current_status in allowed if current_status is not None else None
    return LearningTimelineEntry(
        id=entry.id,
        occurred_at=entry.occurred_at,
        category=entry.category,
        related_domain=entry.related_domain,
        title=entry.title,
        summary=entry.summary,
        confidence_before=entry.confidence_before,
        confidence_after=entry.confidence_after,
        current_status=current_status,
        is_current=is_current,
    )
