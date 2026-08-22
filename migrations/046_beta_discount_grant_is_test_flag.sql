-- Migration 046: persist test/real grant classification directly on the
-- grant row, fixing a real bug found while reviewing migration 045:
-- count_real_claimed_slots()/compute_public_rank()/admin's
-- list_beta_discount_grants() all classified a grant as test-vs-real via
-- a LIVE join to vt_users.full_name. That join breaks PERMANENTLY the
-- moment account_deletion.py::purge_all_user_data() hard-deletes the
-- vt_users row -- which is the exact intended lifecycle for a QA test
-- account (grant row kept for GoBD, user row purged). Once the vt_users
-- row is gone, full_name can never be looked up again, so the grant would
-- silently start counting as a real slot forever. Discovered 2026-08-22,
-- before any account was actually deleted -- no bad data was ever public.
--
-- STATUS: written, NOT yet executed against production Supabase. Must be
-- run, then verified, BEFORE the qa-test-screenshot-demo@example.com
-- account is renamed/screenshotted/deleted (founder's explicit ordering
-- requirement, 2026-08-22).
alter table public.vt_beta_discount_grants
  add column if not exists is_test_grant boolean not null default false;

-- Backfill by FIXED email address, not by re-deriving from the CURRENT
-- full_name -- that name is about to be changed to "Alex" for screenshot
-- purposes, and this backfill must not depend on it.
update public.vt_beta_discount_grants
set is_test_grant = true
where email = 'qa-test-screenshot-demo@example.com';
