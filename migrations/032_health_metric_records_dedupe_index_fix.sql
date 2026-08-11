-- Fix a real, live-proven bug in migration 024: `health_metric_records`
-- (heart-rate/weight) only had a PARTIAL unique index
-- (`where provider_record_name is not null`), but `health_sync_service.py`'s
-- upsert uses `on_conflict="user_id,data_type,provider_record_name"` with no
-- WHERE clause — Postgres cannot infer a partial index from a plain column
-- list, so every real upsert for this table failed with:
--   42P10 "there is no unique or exclusion constraint matching the ON
--   CONFLICT specification"
-- Confirmed live against the founder's real connected Google Health account
-- (a real weight data point was received + normalized correctly but could
-- not be stored because of this exact error).
--
-- A plain (non-partial) unique index changes NOTHING about NULL handling —
-- Postgres unique indexes already treat every NULL as distinct from every
-- other NULL by default, identical to the old partial index's practical
-- behavior — it only makes `ON CONFLICT (user_id, data_type,
-- provider_record_name)` resolvable. `health_activity_records` and
-- `health_sleep_records` (migration 024) already use plain, non-partial
-- unique indexes and have never hit this error.
--
-- Safe to run multiple times, non-destructive, no data changes.

drop index if exists public.idx_health_metric_records_dedupe;

create unique index if not exists idx_health_metric_records_dedupe
  on public.health_metric_records (user_id, data_type, provider_record_name);

notify pgrst, 'reload schema';
