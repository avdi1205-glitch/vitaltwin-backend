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
CALC_TABLE = "vt_twin_calculations"
CGM_TABLE = "vt_cgm_readings"
NUTRITION_TABLE = "vt_nutrition_entries"

# Tables keyed directly by `email` (safe to delete in any order — none of
# these are referenced by a foreign id from another table in this list).
#
# DELIBERATELY EXCLUDED (dependency analysis, plan-architecture/admin round):
# - `vt_audit_events`: kept as a security/compliance log, not user-facing
#   data — unchanged, pre-existing decision.
# - `vt_stripe_subscriptions`/`vt_stripe_payments`/`vt_stripe_refunds`: real
#   invoices/payments may be subject to statutory retention (German
#   HGB/AO bookkeeping rules commonly require ~10 years) — never blindly
#   deleted here. A future anonymization pass (keep the financial record,
#   strip the email) would need explicit legal/accounting sign-off from the
#   founder; not invented here.
# - `vt_contact_messages`/`vt_beta_applications`: independent business
#   records (a contact message or beta application isn't necessarily tied
#   to a registered account — e.g. a prospect who never registered) — same
#   category as the audit-log exception, not part of "this account's data".
# - Google Health tables (`user_health_connections`, `health_oauth_states`,
#   `health_sync_runs`, `health_activity_records`, `health_sleep_records`,
#   `health_metric_records`): NOT listed here on purpose — all six have
#   `user_id bigint references vt_users(id) ON DELETE CASCADE` (migration
#   024), so deleting the `vt_users` row below already removes every one of
#   them at the database level. Listing them again here would be redundant
#   (and they're keyed by `user_id`, not `email`, so they don't fit this
#   email-scoped loop anyway).
# - Family Foundation tables (`vt_families`, `vt_family_members`): same
#   reasoning — both have `user_id`/`owner_user_id bigint references
#   vt_users(id) ON DELETE CASCADE` (migration 029), so a deleted user's
#   own family (if they were the owner) and their membership row in any
#   family are removed automatically. Deleting an owner does NOT delete
#   other members' own VitalTwin accounts/data — only the roster rows.
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
    CALC_TABLE,
    CGM_TABLE,
    NUTRITION_TABLE,
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

    # A deleted account must not still be able to log in via a stale
    # in-process cache entry (`routers/users.py::users_store`) — see
    # `invalidate_cached_user`'s docstring for the real bug this closes.
    from ..routers.users import invalidate_cached_user  # local import: breaks core<->routers cycle

    invalidate_cached_user(email)

    return deleted
