-- Fix a real, live-proven bug found during Health Connect Phase 2.2's own
-- production E2E verification (real synthetic test data written via the
-- Health Connect WRITE API, then synced against production):
--
-- Migration 035 made `connection_id` nullable on `health_activity_records`
-- so Health Connect (no OAuth "connection" concept) could write into it,
-- but did NOT do the same for `health_metric_records` or
-- `health_sleep_records` — both still have `connection_id bigint not null`.
-- Every real Health Connect upsert into these two tables therefore fails
-- with:
--   23502 "null value in column \"connection_id\" of relation
--   \"health_metric_records\"/\"health_sleep_records\" violates not-null
--   constraint"
-- Confirmed live: heart-rate/resting-heart-rate/HRV/SpO2/respiratory-rate/
-- body-temperature/weight (all health_metric_records) and sleep-session
-- (health_sleep_records) all failed with this exact error against
-- production; steps/distance/active-calories/total-calories/
-- exercise-session (all health_activity_records, already fixed by 035)
-- stored correctly.
--
-- Same safe pattern as migration 035: makes the column nullable, does not
-- touch any existing row, does not change Google Health's own behavior
-- (Google Health always supplies a real connection_id).
--
-- Safe to run multiple times, non-destructive, no data changes.
-- NOT YET APPLIED — per Constitution rule (STOP before applying a new
-- production migration automatically), this file must be run manually by
-- the founder in the Supabase SQL Editor before Health Connect sync for
-- metric/sleep data types will work against production.

alter table public.health_metric_records
  alter column connection_id drop not null;

alter table public.health_sleep_records
  alter column connection_id drop not null;

notify pgrst, 'reload schema';
