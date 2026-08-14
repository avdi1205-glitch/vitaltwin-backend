-- Beta Application review workflow: adds a review/approval status directly
-- onto the EXISTING `vt_beta_applications` table (customer self-service
-- applications, migration 010) — deliberately NOT a second table/system.
-- This is what actually connects a customer's application to the EXISTING
-- admin-controlled Beta Tester Program overlay (`vt_users.beta_plan` etc.,
-- migration 034): the one-click "Beta freigeben" admin action reads/writes
-- this status and then calls the EXISTING `grant_beta_by_email()` — no new
-- entitlement mechanism.
--
-- Non-destructive: only `add column if not exists` + a check constraint.
-- Every existing row defaults to 'pending' (their real, honest state — none
-- of them have ever been reviewed through any mechanism before this).

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

notify pgrst, 'reload schema';
