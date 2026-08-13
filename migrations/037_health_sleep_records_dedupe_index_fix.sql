-- Fix the SAME partial-unique-index bug migrations 032/036 already fixed
-- for health_metric_records/health_activity_records, now for
-- health_sleep_records — found while building Health Connect Phase 2.2
-- (on-device SleepSessionRecord + stage sync), BEFORE it could cause a
-- live 42P10 error like the other two tables did.
--
-- health_sleep_records still has a PARTIAL unique index
-- (`where provider_record_name is not null`, migration 024), but every
-- upsert into this table (Google Health's `health_sync_service.py` today,
-- and the new Health Connect sleep-session sync) uses
-- `on_conflict="user_id,provider_record_name"` with no WHERE clause —
-- Postgres cannot infer a partial index from a plain column list, so any
-- real upsert here would fail with the same:
--   42P10 "there is no unique or exclusion constraint matching the ON
--   CONFLICT specification"
-- Migration 036's own comment already flagged this exact table as a
-- "very likely affected, but intentionally not touched" follow-up.
--
-- A plain (non-partial) unique index changes NOTHING about NULL handling —
-- Postgres unique indexes already treat every NULL as distinct from every
-- other NULL by default, identical to the old partial index's practical
-- behavior — it only makes `ON CONFLICT (user_id, provider_record_name)`
-- resolvable.
--
-- Safe to run multiple times, non-destructive, no data changes.
-- NOT YET APPLIED — per Constitution rule (STOP before applying a new
-- production migration automatically), this file must be run manually by
-- the founder in the Supabase SQL Editor before Health Connect sleep sync
-- is used against production.

drop index if exists public.idx_health_sleep_records_dedupe;

create unique index if not exists idx_health_sleep_records_dedupe
  on public.health_sleep_records (user_id, provider_record_name);

notify pgrst, 'reload schema';
