-- ROOT CAUSE REPAIR: `public.vt_beta_applications` was NEVER actually created
-- by any migration in this repo's history. Migration 001's header comment
-- ("vt_marker_reference, vt_beta_applications are all untouched") incorrectly
-- assumed it already pre-existed in production — verified false via a
-- read-only Supabase schema check (0 tables matching %beta%/%application%/
-- %bewerb%/%tester%). This is why `POST /api/beta/apply` has been returning
-- 500 in production: `routers/beta.py`'s INSERT (and every other read/write
-- against this table) has always targeted a table that never existed.
--
-- This migration creates the table with EXACTLY the columns the
-- already-deployed code expects (`routers/beta.py::BetaApplicationRequest`
-- + `_db_store_application`/`_db_has_application`/`_db_application_status`,
-- `routers/admin.py::list_beta_applications`/`approve_beta_application`/
-- `reject_beta_application`) — including the `status`/`reviewed_at`/
-- `reviewed_by` columns migration 039 tried to ADD to a table that didn't
-- exist yet (which is why 039 itself failed). Those 3 columns are folded in
-- here directly so migration 039 is no longer needed (harmless/no-op if run
-- afterward anyway, since it only ever uses `add column if not exists`).
--
-- `id` is `bigint generated always as identity` (matches this repo's own
-- established convention for integer-keyed tables — see migrations
-- 022/023/024/029/030/031) since `routers/admin.py`'s approve/reject
-- endpoints take `application_id: int`, not a UUID.
--
-- Non-destructive: `create table if not exists` only, no existing table
-- touched, no data anywhere deleted or rewritten.
--
-- SECURITY (RLS): this table holds customer PII (full_name/email/age/
-- motivation). RLS is ENABLED with ZERO policies for `anon`/`authenticated`
-- — a direct anon request can never INSERT/SELECT/UPDATE/DELETE this table
-- at all. The only way in is `app/core/supabase.py`'s new `supabase_admin`
-- client, built from the `SUPABASE_SERVICE_ROLE_KEY` env var — a real
-- Postgres/Supabase service-role key always bypasses RLS entirely by
-- design, so it needs (and gets) no policy of its own here. Every
-- `vt_beta_applications` operation in `routers/beta.py` (insert/select) and
-- `routers/admin.py` (list/approve/reject) has been switched to use that
-- client instead of the plain anon `supabase` client used everywhere else
-- in this codebase — all of them fail closed (503) if the env var isn't
-- set yet, rather than silently falling back to anon access. FastAPI's own
-- `require_admin_permission(..., "manage_premium")` check on the admin
-- endpoints is UNCHANGED and still mandatory — the service-role client
-- only decides what the BACKEND PROCESS itself can reach in Postgres, it
-- does not replace or weaken the existing per-request admin authorization.

create table if not exists public.vt_beta_applications (
  id bigint generated always as identity primary key,
  full_name text not null,
  email text not null,
  age integer null,
  motivation text not null,
  source text null,
  status text not null default 'pending',
  reviewed_at timestamptz null,
  reviewed_by text null,
  created_at timestamptz not null default now()
);

alter table public.vt_beta_applications
  drop constraint if exists vt_beta_applications_status_check;
alter table public.vt_beta_applications
  add constraint vt_beta_applications_status_check check (status in ('pending', 'approved', 'rejected'));

-- Supports `_db_has_application`/`_db_application_status`/`beta_application_status`'s
-- `.eq("email", ...).limit(1)` lookups (not unique — the existing code only
-- ever reads the first match, never assumed hard DB-level uniqueness).
create index if not exists idx_vt_beta_applications_email
  on public.vt_beta_applications(email);

-- Supports the admin list view's `.order("created_at", desc=True)`.
create index if not exists idx_vt_beta_applications_created_at
  on public.vt_beta_applications(created_at desc);

-- Supports migration 039's originally-intended status index (same purpose,
-- folded in here since 039 never actually ran against a real table).
create index if not exists idx_vt_beta_applications_status
  on public.vt_beta_applications(status);

alter table public.vt_beta_applications enable row level security;

-- Deliberately ZERO policies for anon/authenticated: no direct table
-- access is possible at all except via the service-role client, which
-- bypasses RLS by design and therefore needs no policy of its own.
drop policy if exists vt_beta_applications_anon_insert on public.vt_beta_applications;
drop policy if exists vt_beta_applications_anon_select on public.vt_beta_applications;
drop policy if exists vt_beta_applications_anon_update on public.vt_beta_applications;

notify pgrst, 'reload schema';
