-- Real Tier Architecture Foundation (VitalTwin Plan System).
--
-- Adds a real `plan` column to `vt_users` so Free/Premium/Pro/Family can
-- finally be distinguished server-side — previously only a boolean
-- `premium` flag existed, meaning Pro/Family subscribers were technically
-- indistinguishable from Premium everywhere in the backend (see
-- `app/core/plan_service.py` docstring for the full architecture).
--
-- TRANSITION-SAFE BY DESIGN: the old `premium` boolean is NOT removed or
-- renamed here — it keeps working exactly as before for any code path not
-- yet migrated to `plan`. Both columns are kept in sync going forward by
-- `app/core/plan_service.py::set_plan_by_email()` /
-- `app/routers/users.py::set_premium_by_email()`. The old column should
-- only be dropped in a later migration once every caller has been
-- confirmed to read `plan` instead.
--
-- Non-destructive: only `add column if not exists`, a one-time backfill
-- `update` (idempotent — re-running it is a no-op the second time since the
-- `where` clause only matches rows still at the default), and
-- `create index if not exists`. No existing row is deleted, no existing
-- column is dropped or renamed.

alter table public.vt_users
  add column if not exists plan text not null default 'free';

-- Backfill existing users from the legacy boolean (one-time, idempotent):
-- premium = true  -> plan = 'premium' (matches this project's established
--                    fact that pro/family were never distinguishable from
--                    premium in the DB before this migration — there is no
--                    real signal anywhere to backfill them as pro/family
--                    instead, so 'premium' is the only honest choice here).
-- premium = false -> plan stays 'free' (already the column default).
update public.vt_users
set plan = 'premium'
where premium = true and plan = 'free';

-- Enforce the allowed value set at the database level too (defense in
-- depth alongside the application-level `PlanId` check).
alter table public.vt_users
  drop constraint if exists vt_users_plan_check;
alter table public.vt_users
  add constraint vt_users_plan_check check (plan in ('free', 'premium', 'pro', 'family'));

create index if not exists idx_vt_users_plan on public.vt_users(plan);
