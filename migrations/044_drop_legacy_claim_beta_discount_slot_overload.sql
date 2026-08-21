-- Migration 044: drop the stale 3-parameter overload of
-- claim_beta_discount_slot() left behind in production after migration
-- 043 was updated (2026-08-21) to add a 4th parameter (`p_expires_at`).
-- `create or replace function` cannot change an existing function's
-- parameter list -- adding a parameter without a default creates a
-- SECOND, separate overload rather than replacing the old one. Confirmed
-- live via `pg_get_function_identity_arguments` returning 2 rows for
-- `claim_beta_discount_slot` (both signatures pasted verbatim from that
-- real query result, not guessed):
--   OLD (to drop): p_email text, p_first_real_usage_at timestamp with
--                  time zone, p_first_real_usage_source text
--   NEW (keep):    p_email text, p_first_real_usage_at timestamp with
--                  time zone, p_first_real_usage_source text,
--                  p_expires_at timestamp with time zone
--
-- STATUS: written, NOT yet executed against production Supabase -- run
-- manually in the Supabase SQL Editor. Review before running.
--
-- SAFETY: matches on the exact confirmed old signature STRING (not just
-- parameter count) via pg_get_function_identity_arguments -- aborts with
-- a clear exception if zero or more than one match is found, rather than
-- silently doing nothing or dropping something unintended. A prior
-- manual DROP attempt (hand-typed signature) silently failed to match --
-- this anchors on Postgres's own catalog-reported signature string
-- instead of a second hand-typed guess, eliminating that failure mode.
-- Cannot touch the NEW 4-parameter overload: its identity-arguments
-- string is necessarily different (one more parameter), so the WHERE
-- clause below can never match it.

-- STEP 1 (read-only) -- run first, confirm you see exactly 2 rows
-- (3-param and 4-param) before running the DO block below:
select oid, pronargs, pg_get_function_identity_arguments(oid) as signature
from pg_proc
where proname = 'claim_beta_discount_slot'
  and pronamespace = 'public'::regnamespace;

-- STEP 2 -- drop ONLY the confirmed-old 3-parameter overload.
do $$
declare
  v_old_oid oid;
  v_expected_old_signature text :=
    'p_email text, p_first_real_usage_at timestamp with time zone, p_first_real_usage_source text';
begin
  select oid into v_old_oid
  from pg_proc
  where proname = 'claim_beta_discount_slot'
    and pronamespace = 'public'::regnamespace
    and pg_get_function_identity_arguments(oid) = v_expected_old_signature;

  if v_old_oid is null then
    raise exception 'No function found matching the confirmed old signature (%). Nothing dropped -- re-check pg_proc manually before retrying.', v_expected_old_signature;
  end if;

  execute format('drop function public.claim_beta_discount_slot(%s)', v_expected_old_signature);

  raise notice 'Dropped old claim_beta_discount_slot overload (oid %, signature: %).', v_old_oid, v_expected_old_signature;
end $$;

-- STEP 3 (verification, read-only) -- re-run the same check as before.
-- Expect exactly 1 row now, the 4-parameter version.
select pg_get_function_identity_arguments(oid) as signature
from pg_proc
where proname = 'claim_beta_discount_slot'
  and pronamespace = 'public'::regnamespace;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
