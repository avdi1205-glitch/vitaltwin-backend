-- Migration 045: compensate for the 1 real production discount slot
-- (raw slot_number=1) wasted on an internal QA screenshot-demo test
-- account (qa-test-screenshot-demo@example.com) discovered 2026-08-22.
-- Anti-gaming behavior confirmed correct (detect_first_real_action() keys
-- off real DB insert time, not a caller-backdated date) -- this is NOT a
-- bug fix, just a compensating adjustment so 20 REAL external testers can
-- still receive slots 1-20 (computed public rank, not the raw DB
-- slot_number -- see beta_discount_program.py::compute_public_rank).
--
-- *** DO NOT RUN THIS BEFORE THE NEW BACKEND CODE (compute_public_rank /
-- count_real_claimed_slots / qa_test_accounts.py) IS DEPLOYED AND
-- CONFIRMED LIVE. *** Run only AFTER confirming the deploy succeeded --
-- founder's explicit ordering requirement, 2026-08-22.
--
-- STATUS: written, NOT yet executed against production Supabase.
--
-- WHY +1, EXACTLY: the test account's claim already consumed raw sequence
-- value 1. Raising MAXVALUE from 20 to 21 makes room for exactly 20 MORE
-- real nextval() calls (values 2..21) -- since the test grant is excluded
-- from every public count/rank calculation, those 20 real grantees will
-- show computed public ranks 1..20, exactly matching the existing public
-- "first 20" promise. TOTAL_DISCOUNT_SLOTS (the Python/public-facing
-- constant, still 20) is DELIBERATELY NOT changed -- this migration only
-- adjusts the internal DB-level allocation ceiling, never the public
-- number shown anywhere.
--
-- SAFETY (confirmed, standard PostgreSQL DDL semantics): `alter sequence
-- ... maxvalue N` changes ONLY the upper bound enforced by FUTURE
-- nextval() calls. It does not alter the sequence's current value, does
-- not touch any existing table row, and cannot renumber/shift any
-- already-issued value -- non-destructive by construction.
alter sequence public.beta_discount_slot_seq maxvalue 21;

-- Mark the existing test grant row so it's unambiguous on direct
-- inspection too, not just via the qa-test-/QA-TEST-ACCOUNT pattern match
-- the app already applies at query time. Does not change status, slot
-- number, or any other field -- the row is deliberately left in place
-- (per explicit instruction: no existing row is moved/renumbered).
update public.vt_beta_discount_grants
set notes = 'Interner QA-Test (Screenshot-Demo-Account) -- zaehlt nicht als oeffentlicher Beta-Slot, siehe core/qa_test_accounts.py.'
where email = 'qa-test-screenshot-demo@example.com';
