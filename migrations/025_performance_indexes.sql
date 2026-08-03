-- Performance pass (2026-08-03): add the two genuinely missing indexes
-- identified while auditing real hot-path queries — NOT a blind index
-- sweep. Every index below is backed by an actual `.eq()`/`.in_()` filter
-- found in application code that had no matching index yet:
--
-- 1. `vt_wellness_goals.email` — `daily_planning.py` filters this table by
--    email on the daily-planning hot path (`.eq("email", email)`), but the
--    table only had `user_id`-based indexes (migration 003) — a sequential
--    scan on every call.
-- 2. `vt_recommendation_decisions/_outcomes/_feedback.user_id` — these 3
--    tables previously had NO `user_id` index at all (only
--    `recommendation_id`). Added defensively alongside the
--    `profile.py::export_profile` fix that now correctly reads these
--    tables via their `recommendation_id` FK (see that file's comment for
--    why filtering them by `email` never worked — they have no `email`
--    column) — a `user_id` index protects any other current/future code
--    path that looks up "this user's decisions/outcomes/feedback" directly.
--
-- `create index if not exists` only — safe to run multiple times, adds no
-- new tables/columns, changes no existing data.

create index if not exists idx_vt_wellness_goals_email on public.vt_wellness_goals(email);

create index if not exists idx_vt_recommendation_decisions_user_id
  on public.vt_recommendation_decisions(user_id);
create index if not exists idx_vt_recommendation_outcomes_user_id
  on public.vt_recommendation_outcomes(user_id);
create index if not exists idx_vt_recommendation_feedback_user_id
  on public.vt_recommendation_feedback(user_id);

notify pgrst, 'reload schema';
