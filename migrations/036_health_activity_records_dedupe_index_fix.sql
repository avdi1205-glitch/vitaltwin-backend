-- Fix a real, live-proven bug (found during Health Connect Phase 2.1's own
-- live verification): `health_activity_records` (steps/distance/
-- active-minutes) only ever had a PARTIAL unique index
-- (`where provider_record_name is not null`, migration 024), but every
-- upsert (Google Health's `health_sync_service.py` AND the new Health
-- Connect `health_connect.py`) uses
-- `on_conflict="user_id,data_type,provider_record_name"` with no WHERE
-- clause — Postgres cannot infer a partial index from a plain column list,
-- so every real upsert into this table failed with:
--   42P10 "there is no unique or exclusion constraint matching the ON
--   CONFLICT specification"
-- Confirmed live via a real POST to /api/health/health-connect/sync against
-- production (this exact error message was returned). Migration 032's own
-- comment incorrectly claimed this table already used a plain index —
-- that claim was never actually true for this table; migration 024's SQL
-- shows the same `where provider_record_name is not null` partial index
-- health_metric_records had before migration 032 fixed it. Same fix here.
--
-- A plain (non-partial) unique index changes NOTHING about NULL handling —
-- Postgres unique indexes already treat every NULL as distinct from every
-- other NULL by default, identical to the old partial index's practical
-- behavior — it only makes `ON CONFLICT (user_id, data_type,
-- provider_record_name)` resolvable.
--
-- health_sleep_records has the SAME partial-index pattern (migration 024)
-- and is very likely affected too, but is intentionally NOT touched here —
-- out of scope for Health Connect Phase 2 (steps only); flagged as a real,
-- separate follow-up.
--
-- Safe to run multiple times, non-destructive, no data changes.

drop index if exists public.idx_health_activity_records_dedupe;

create unique index if not exists idx_health_activity_records_dedupe
  on public.health_activity_records (user_id, data_type, provider_record_name);

notify pgrst, 'reload schema';
