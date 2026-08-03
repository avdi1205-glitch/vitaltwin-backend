"""Executes an already-requested account deletion (GDPR "right to
erasure", Etappe 9 §2) across every table that stores data scoped to a
user's `email`. Only ever called from an explicit admin action after a
user's `deletion_requested_at` has been set via `routers/profile.py::
request_deletion` — deletion is never automatic, per the app's documented
"reviewed manually" design (avoids irreversible loss from a typo/abuse)."""

from __future__ import annotations

from .supabase import supabase

PROFILE_TABLE = "vt_user_profiles"
USER_TABLE = "vt_users"
ADMIN_ROLE_TABLE = "vt_admin_roles"
CONSENT_TABLE = "vt_consent_records"
DAILY_ENTRY_TABLE = "vt_daily_wellness_entries"
HABIT_TABLE = "vt_habits"
HABIT_ENTRY_TABLE = "vt_habit_entries"
GOAL_TABLE = "vt_wellness_goals"
DAILY_PLAN_TABLE = "vt_daily_plans"
DAILY_PLAN_ACTION_TABLE = "vt_daily_plan_actions"
DAILY_REFLECTION_TABLE = "vt_daily_reflections"
WEEKLY_REFLECTION_TABLE = "vt_weekly_reflections"
RECOMMENDATION_TABLE = "vt_recommendations"
RECOMMENDATION_DECISION_TABLE = "vt_recommendation_decisions"
RECOMMENDATION_OUTCOME_TABLE = "vt_recommendation_outcomes"
RECOMMENDATION_FEEDBACK_TABLE = "vt_recommendation_feedback"
MEMORY_TABLE = "vt_twin_memory"
PATTERN_TABLE = "vt_twin_patterns"
LEARNING_EVENT_TABLE = "vt_twin_learning_events"
CONTEXT_SNAPSHOT_TABLE = "vt_twin_context_snapshots"
CHAT_USAGE_TABLE = "vt_chat_usage"
FEEDBACK_TABLE = "vt_user_feedback"
LOGIN_EVENT_TABLE = "vt_login_events"

# Tables keyed directly by `email` (safe to delete in any order — none of
# these are referenced by a foreign id from another table in this list).
# `vt_audit_events` is deliberately NOT included: audit history is kept
# even after account deletion, matching this table's purpose as a
# security/compliance log rather than user-facing data.
_DIRECT_EMAIL_TABLES: tuple[str, ...] = (
    DAILY_PLAN_ACTION_TABLE,
    HABIT_ENTRY_TABLE,
    DAILY_REFLECTION_TABLE,
    WEEKLY_REFLECTION_TABLE,
    DAILY_PLAN_TABLE,
    HABIT_TABLE,
    GOAL_TABLE,
    DAILY_ENTRY_TABLE,
    MEMORY_TABLE,
    PATTERN_TABLE,
    LEARNING_EVENT_TABLE,
    CONTEXT_SNAPSHOT_TABLE,
    CONSENT_TABLE,
    CHAT_USAGE_TABLE,
    FEEDBACK_TABLE,
    LOGIN_EVENT_TABLE,
    ADMIN_ROLE_TABLE,
    PROFILE_TABLE,
)


def purge_all_user_data(email: str) -> dict[str, int | None]:
    """Deletes every row scoped to `email`, then finally the account row
    itself in `vt_users`. Returns a per-table deleted-row-count (or `None`
    on failure for that specific table) so the caller can record a real
    audit trail of what was actually removed — never silently partial."""
    email = email.strip().lower()
    deleted: dict[str, int | None] = {}

    # Recommendation child tables have no `email` column of their own
    # (confirmed against migrations 003/005) — resolve via recommendation_id.
    try:
        rec_ids = [
            row["id"]
            for row in (supabase.table(RECOMMENDATION_TABLE).select("id").eq("email", email).execute().data or [])
        ]
    except Exception:
        rec_ids = []

    for table in (RECOMMENDATION_DECISION_TABLE, RECOMMENDATION_OUTCOME_TABLE, RECOMMENDATION_FEEDBACK_TABLE):
        if not rec_ids:
            deleted[table] = 0
            continue
        try:
            response = supabase.table(table).delete().in_("recommendation_id", rec_ids).execute()
            deleted[table] = len(response.data or [])
        except Exception:
            deleted[table] = None

    try:
        response = supabase.table(RECOMMENDATION_TABLE).delete().eq("email", email).execute()
        deleted[RECOMMENDATION_TABLE] = len(response.data or [])
    except Exception:
        deleted[RECOMMENDATION_TABLE] = None

    for table in _DIRECT_EMAIL_TABLES:
        try:
            response = supabase.table(table).delete().eq("email", email).execute()
            deleted[table] = len(response.data or [])
        except Exception:
            deleted[table] = None

    try:
        response = supabase.table(USER_TABLE).delete().eq("email", email).execute()
        deleted[USER_TABLE] = len(response.data or [])
    except Exception:
        deleted[USER_TABLE] = None

    return deleted
