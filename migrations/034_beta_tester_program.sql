-- Beta Tester Program: admin-controlled, time-limited Pro/Family (and
-- Premium) access overlay — distinct from `vt_users.plan` (the real,
-- underlying paid/free plan).
--
-- Deliberately added as columns on the EXISTING `vt_users` table (same
-- pattern as `suspended`/`suspended_at`/`suspended_reason`) rather than a
-- second table: only ever one active grant per user is needed, and the
-- effective-plan resolver (`core/plan_service.py::get_effective_plan_by_email`)
-- is called on nearly every gated request, so keeping it a single-row read
-- (no join) matters. History of past grants/revocations is kept via the
-- EXISTING `vt_audit_events` table (`record_audit_event`), not extra rows
-- here — matches the established pattern used for suspend/unsuspend/plan
-- changes elsewhere in `routers/admin.py`.
--
-- Non-destructive: only `add column if not exists` + a check constraint +
-- an index for the admin "expiring soon" overview. No existing column is
-- touched, no existing row is modified.

alter table public.vt_users
  add column if not exists beta_plan text null,
  add column if not exists beta_started_at timestamptz null,
  add column if not exists beta_expires_at timestamptz null,
  add column if not exists beta_granted_by text null;

alter table public.vt_users
  drop constraint if exists vt_users_beta_plan_check;
alter table public.vt_users
  add constraint vt_users_beta_plan_check check (beta_plan is null or beta_plan in ('premium', 'pro', 'family'));

-- Supports the admin "expiring soon / expired" overview query
-- (`WHERE beta_plan IS NOT NULL ORDER BY beta_expires_at`) without a full
-- table scan.
create index if not exists idx_vt_users_beta_expires_at
  on public.vt_users(beta_expires_at)
  where beta_plan is not null;

notify pgrst, 'reload schema';
