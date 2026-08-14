-- SUPERSEDED by migration 040 (`040_beta_applications_table.sql`): this
-- migration incorrectly assumed `public.vt_beta_applications` already
-- existed in production (it never did — no migration ever created it,
-- confirmed via a read-only schema check). Migration 040 creates the table
-- from scratch WITH these exact 3 columns already included, so this file no
-- longer needs to be run on its own. Kept in place (not deleted) since it
-- is fully idempotent (`add column if not exists`) and harmless to run
-- after 040 — it will simply no-op.
--
-- FRESH-DATABASE SAFETY: migrations always run in file order, so on a
-- brand-new database this file executes BEFORE 040 creates the table —
-- `alter table ... add column if not exists` would fail outright with
-- "relation does not exist" in that case (unlike a real column, Postgres
-- has no `if exists` guard for the TABLE itself in a plain ALTER). Every
-- statement below is now wrapped in a `do $$ ... end $$` block gated on
-- `to_regclass('public.vt_beta_applications') is not null`, so on a fresh
-- database this migration safely no-ops (010/040 will do the real work
-- later in file order), and on a database where the table already exists
-- it behaves exactly as before.
--
-- Beta Application review workflow: adds a review/approval status directly
-- onto the `vt_beta_applications` table (customer self-service
-- applications) — deliberately NOT a second table/system.
-- This is what actually connects a customer's application to the EXISTING
-- admin-controlled Beta Tester Program overlay (`vt_users.beta_plan` etc.,
-- migration 034): the one-click "Beta freigeben" admin action reads/writes
-- this status and then calls the EXISTING `grant_beta_by_email()` — no new
-- entitlement mechanism.
--
-- Non-destructive: only `add column if not exists` + a check constraint.
-- Every existing row defaults to 'pending' (their real, honest state — none
-- of them have ever been reviewed through any mechanism before this).

do $$
begin
  if to_regclass('public.vt_beta_applications') is not null then
    alter table public.vt_beta_applications
      add column if not exists status text not null default 'pending',
      add column if not exists reviewed_at timestamptz null,
      add column if not exists reviewed_by text null;

    alter table public.vt_beta_applications
      drop constraint if exists vt_beta_applications_status_check;
    alter table public.vt_beta_applications
      add constraint vt_beta_applications_status_check check (status in ('pending', 'approved', 'rejected'));

    create index if not exists idx_vt_beta_applications_status
      on public.vt_beta_applications(status);
  end if;
end $$;

notify pgrst, 'reload schema';
