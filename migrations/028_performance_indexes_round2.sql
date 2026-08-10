-- Performance pass round 2 (customer-area sweep, 2026-08-10): one genuinely
-- verified missing composite index — NOT a blind index sweep.
--
-- `vt_daily_wellness_entries` is the single hottest table in the customer
-- area: read on every /dashboard, /dashboard/gewohnheiten, /dashboard/verlauf
-- and /frag-deinen-twin request via `profile.py::list_daily_entries`,
-- `get_today_entry`, `get_trends`, `get_personal_baseline`, and
-- `chat.py::_build_context_for_user` — every one of these filters
-- `.eq("email", email).order("entry_date", desc=True)`. Migration 001 only
-- added a single-column index on `email`; migration 003 added a composite
-- index on `(user_id, entry_date)`, not `(email, entry_date)`. Without a
-- matching composite index, Postgres can use the `email` index to find this
-- user's rows but must still sort them by `entry_date` separately every
-- time — a real, growing cost as each user's row count increases.
--
-- `create index if not exists` only — safe to run multiple times, adds no
-- new tables/columns, changes no existing data. NOT yet run in Supabase.
create index if not exists idx_vt_daily_wellness_entries_email_entry_date
  on public.vt_daily_wellness_entries(email, entry_date desc);

notify pgrst, 'reload schema';
