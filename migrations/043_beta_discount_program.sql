-- Migration 043: "First 20 active beta testers" discount program
-- foundation — 50% off Premium/Pro/Family for 6 months, awarded once per
-- person to the first 20 users whose earliest real in-app action
-- (check-in, twin calculation, or Health Connect sync) occurs on or after
-- the program's launch instant. The read/write logic on top of this lives
-- in `backend/app/core/beta_discount_program.py`; this migration only adds
-- the sequence + table it uses.
--
-- STATUS: written, NOT yet executed against production Supabase — run
-- manually in the Supabase SQL Editor, same convention as every other
-- migration in this repo. Review the `claim_beta_discount_slot()` function
-- below before running.
--
-- WHY A SEQUENCE (race-condition-safe slot allocation): supabase-py has no
-- raw SQL connection, so a true `SELECT ... FOR UPDATE`/`pg_advisory_lock`
-- is not cleanly reachable from application code (same limitation already
-- documented for the Health Connect token-refresh lock, migration-041-era
-- notes). A capped Postgres SEQUENCE (`nextval()`) is atomic by
-- construction — Postgres itself serializes concurrent calls across
-- different backend processes/threads — and raises once exhausted,
-- giving a natural race-free hard cap at 20 with zero application-level
-- locking. `claim_beta_discount_slot()` is the FIRST stored function/RPC
-- call used anywhere in this codebase — introduced specifically because
-- ordinary REST-only upsert/insert calls cannot express "insert only if
-- fewer than N rows exist" atomically.
--
-- Non-destructive: only `create table if not exists`/`create sequence if
-- not exists`/`create or replace function`, no existing table touched, no
-- data anywhere deleted or rewritten.
--
-- SECURITY (RLS): disabled, same convention as vt_stripe_*/vt_adsense_* —
-- access controlled entirely at the FastAPI layer. If the RPC call from
-- the plain anon-key client is ever rejected with a permissions error,
-- run `grant execute on function public.claim_beta_discount_slot(text,
-- timestamptz, text, timestamptz) to anon, authenticated;` once as a
-- follow-up.
--
-- GDPR RETENTION (GoBD-Prinzip check): deliberately excluded from
-- `account_deletion.py::purge_all_user_data` — same statutory-retention
-- reasoning as vt_stripe_*, since a grant row explains a real invoice's
-- reduced price. See that file's own exclusion-reasoning comment block.
--
-- EXPIRATION (2026-08-21 decision): `expires_at` is set on insert to
-- `first_real_usage_at`'s claim moment + 12 months (computed in Python,
-- passed as `p_expires_at` — kept in the app layer rather than a second
-- SQL default so both the DB row and the Stripe Promotion Code's own
-- `expires_at` share the exact same instant). A grant not converted into
-- a real checkout within that window lazily flips to `status='expired'`
-- the next time it's read (`core/beta_discount_program.py::
-- _expire_if_past_due`) — the slot_number is NEVER recycled either way,
-- the sequence only ever increments.
--
-- ADMIN/TEST-ACCOUNT EXCLUSION: `BETA_DISCOUNT_EXCLUDED_EMAILS` (backend
-- env var, comma-separated, case-insensitive) is checked first, in
-- Python, before this function is ever called — not enforced here in SQL
-- (keeps the one env var as the single source of truth, no schema
-- coupling to a value that may change without a deploy).

create sequence if not exists public.beta_discount_slot_seq
  minvalue 1 maxvalue 20 no cycle;

create table if not exists public.vt_beta_discount_grants (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  email text not null,
  slot_number integer not null,
  first_real_usage_at timestamptz not null,
  first_real_usage_source text not null,
  discount_percent integer not null default 50,
  duration_months integer not null default 6,
  stripe_coupon_id text,
  stripe_promotion_code_id text,
  status text not null default 'granted',
  applied_at timestamptz,
  expires_at timestamptz,
  notes text
);

alter table public.vt_beta_discount_grants
  drop constraint if exists vt_beta_discount_grants_status_check;
alter table public.vt_beta_discount_grants
  add constraint vt_beta_discount_grants_status_check
  check (status in ('granted', 'applied', 'expired', 'revoked'));

alter table public.vt_beta_discount_grants
  drop constraint if exists vt_beta_discount_grants_source_check;
alter table public.vt_beta_discount_grants
  add constraint vt_beta_discount_grants_source_check
  check (first_real_usage_source in ('checkin', 'twin_calculation', 'health_connect_sync'));

create unique index if not exists uq_vt_beta_discount_grants_email
  on public.vt_beta_discount_grants (email);
create unique index if not exists uq_vt_beta_discount_grants_slot_number
  on public.vt_beta_discount_grants (slot_number);

alter table public.vt_beta_discount_grants disable row level security;

-- Race-safe atomic slot claim: idempotent per email (a repeat call for an
-- email that already has a grant returns that same grant unchanged,
-- never a second row/slot), never raises past the sequence's own cap — a
-- caller always gets a clean (slot_number, granted) result even once all
-- 20 slots are taken (granted=false, slot_number=null).
--
-- KNOWN, ACCEPTED EDGE CASE: if the SAME user triggers two different
-- qualifying actions (e.g. a check-in and a twin calculation) within the
-- same instant from two concurrent requests, both could pass the
-- "does a grant already exist" check before either commits, both call
-- nextval(), and the second insert then no-ops on the unique email index
-- — at most one sequence value is consumed without a matching grant in
-- this rare case. This does not affect fairness between different users
-- and is an acceptable, documented tradeoff given supabase-py's lack of a
-- raw-SQL row lock (see the migration header above).
create or replace function public.claim_beta_discount_slot(
  p_email text,
  p_first_real_usage_at timestamptz,
  p_first_real_usage_source text,
  p_expires_at timestamptz
) returns table(slot_number integer, granted boolean) as $$
declare
  v_existing_slot integer;
  v_slot integer;
begin
  select g.slot_number into v_existing_slot
    from public.vt_beta_discount_grants g
    where g.email = p_email;

  if v_existing_slot is not null then
    return query select v_existing_slot, true;
    return;
  end if;

  begin
    v_slot := nextval('public.beta_discount_slot_seq');
  exception when others then
    -- Sequence exhausted (all 20 already claimed) or any other allocation
    -- error — never propagate, just report "not granted".
    return query select null::integer, false;
    return;
  end;

  insert into public.vt_beta_discount_grants
    (email, slot_number, first_real_usage_at, first_real_usage_source, expires_at)
  values
    (p_email, v_slot, p_first_real_usage_at, p_first_real_usage_source, p_expires_at)
  on conflict (email) do nothing;

  return query select v_slot, true;
end;
$$ language plpgsql;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
