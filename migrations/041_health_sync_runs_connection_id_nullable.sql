-- Migration 041: Health Connect background sync (Phase 2.3) — makes
-- `health_sync_runs.connection_id` nullable so Health-Connect-sourced sync
-- attempts can be logged the same way Google Health ones already are.
--
-- STATUS: written, NOT yet executed against production Supabase — run
-- manually in the Supabase SQL Editor, same convention as every other
-- migration in this repo.
--
-- WHY THIS IS NEEDED: `health_sync_runs.connection_id` (migration 024) is
-- `bigint not null references user_health_connections(id)` — that FK only
-- ever makes sense for Google Health's OAuth connections. Health Connect
-- (on-device Android, no OAuth) has no `user_health_connections` row at all
-- (confirmed: every row `routers/health_connect.py` writes into the 3
-- canonical data tables already uses `connection_id = null` — this
-- migration brings `health_sync_runs` in line with that same, pre-existing
-- convention, it does not introduce a new one).
--
-- Non-destructive: only relaxes a NOT NULL constraint, never deletes/
-- rewrites data, never touches the foreign key itself (Postgres already
-- exempts NULL values from FK checks by default — existing Google Health
-- rows, which always have a real connection_id, are completely unaffected).
alter table public.health_sync_runs
  alter column connection_id drop not null;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
