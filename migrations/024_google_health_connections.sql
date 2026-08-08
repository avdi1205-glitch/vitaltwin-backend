-- Google Health API integration — production-grade schema (supersedes the
-- earlier draft in this same file; safe to replace since 024 was never run
-- in Supabase). Deliberately uses `bigint generated always as identity`
-- primary keys (not UUID) to stay consistent with every other table in this
-- codebase (vt_users.id, vt_founder_tasks.id, ...) rather than introducing
-- a new PK convention for just this feature.
--
-- All tables start EMPTY — no demo data, no backfill.

-- ---------------------------------------------------------------------------
-- 1. OAuth connections — one active connection per (user, provider).
--    access_token/refresh_token are stored ENCRYPTED (Fernet, see
--    core/health_encryption_service.py) — never plaintext at rest, never
--    returned in any API response.
-- ---------------------------------------------------------------------------
create table if not exists public.user_health_connections (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  provider text not null default 'google_health',
  provider_health_user_id text,
  provider_legacy_user_id text,
  encrypted_access_token text not null,
  encrypted_refresh_token text not null,
  encryption_key_version integer not null default 1,
  token_type text not null default 'Bearer',
  granted_scopes text[] not null default '{}',
  access_token_expires_at timestamptz not null,
  status text not null default 'connected',
  connected_at timestamptz not null default now(),
  reauthorization_required_at timestamptz,
  reauthorization_reason text,
  last_sync_at timestamptz,
  last_sync_status text,
  last_sync_error_code text,
  last_sync_error_message text,
  -- Poor-man's single-flight lock for token refresh (see module docstring in
  -- core/health_token_service.py for why this is NOT a true Postgres
  -- advisory lock — supabase-py only exposes the PostgREST table API, not a
  -- raw SQL connection, so `pg_advisory_lock` isn't reachable from this
  -- codebase's DB access layer).
  refresh_lock_token text,
  refresh_lock_expires_at timestamptz,
  constraint user_health_connections_status_check
    check (status in ('connected', 'disconnected', 'reauthorization_required', 'error')),
  constraint user_health_connections_provider_check
    check (provider in ('google_health'))
);

-- Exactly one active connection per user+provider (disconnected/error rows
-- from a prior connection don't count — see the partial unique index).
create unique index if not exists idx_user_health_connections_active_unique
  on public.user_health_connections (user_id, provider)
  where status in ('connected', 'reauthorization_required');

create index if not exists idx_user_health_connections_user
  on public.user_health_connections (user_id);

alter table public.user_health_connections disable row level security;

-- ---------------------------------------------------------------------------
-- 2. OAuth state — CSRF-safe, single-use, short-lived. The `state` value
--    sent to Google is a cryptographically random opaque token; only its
--    SHA-256 hash is stored here (so a leaked database row alone can't be
--    replayed as a valid state — matches the spec's "State selbst nicht
--    zwingend im Klartext speichern").
-- ---------------------------------------------------------------------------
create table if not exists public.health_oauth_states (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  state_hash text not null unique,
  user_id bigint not null references public.vt_users(id) on delete cascade,
  provider text not null default 'google_health',
  requested_scopes text[] not null default '{}',
  frontend_redirect_path text not null default '/dashboard',
  expires_at timestamptz not null,
  used_at timestamptz
);

create index if not exists idx_health_oauth_states_expires_at
  on public.health_oauth_states (expires_at);

alter table public.health_oauth_states disable row level security;

-- ---------------------------------------------------------------------------
-- 3. Sync runs — one row per sync attempt (manual or, later, scheduled),
--    real counters, real status, never fabricated.
-- ---------------------------------------------------------------------------
create table if not exists public.health_sync_runs (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  connection_id bigint not null references public.user_health_connections(id) on delete cascade,
  provider text not null default 'google_health',
  sync_type text not null default 'manual',
  requested_data_types text[] not null default '{}',
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running',
  records_received integer not null default 0,
  records_created integer not null default 0,
  records_updated integer not null default 0,
  records_skipped integer not null default 0,
  error_code text,
  error_message text,
  metadata jsonb not null default '{}',
  constraint health_sync_runs_status_check
    check (status in ('queued', 'running', 'completed', 'partial', 'failed'))
);

create index if not exists idx_health_sync_runs_user_started
  on public.health_sync_runs (user_id, started_at desc);

alter table public.health_sync_runs disable row level security;

-- ---------------------------------------------------------------------------
-- 4a. Activity records (steps, distance, active minutes, ...) — "Interval"
--     shaped Google Health data types.
-- ---------------------------------------------------------------------------
create table if not exists public.health_activity_records (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  connection_id bigint not null references public.user_health_connections(id) on delete cascade,
  provider text not null default 'google_health',
  provider_record_name text,
  data_type text not null,
  start_time timestamptz not null,
  end_time timestamptz,
  value double precision,
  unit text,
  source_name text,
  source_device text,
  source_app text,
  raw_metadata jsonb,
  observed_at timestamptz,
  imported_at timestamptz not null default now()
);

create index if not exists idx_health_activity_records_user_type_time
  on public.health_activity_records (user_id, data_type, start_time desc);
create unique index if not exists idx_health_activity_records_dedupe
  on public.health_activity_records (user_id, data_type, provider_record_name)
  where provider_record_name is not null;

alter table public.health_activity_records disable row level security;

-- ---------------------------------------------------------------------------
-- 4b. Sleep records — "Session" shaped Google Health data (sleep stages).
-- ---------------------------------------------------------------------------
create table if not exists public.health_sleep_records (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  connection_id bigint not null references public.user_health_connections(id) on delete cascade,
  provider text not null default 'google_health',
  provider_record_name text,
  start_time timestamptz not null,
  end_time timestamptz,
  duration_seconds integer,
  sleep_stage text,
  timezone_offset_minutes integer,
  source_name text,
  raw_metadata jsonb,
  imported_at timestamptz not null default now()
);

create index if not exists idx_health_sleep_records_user_time
  on public.health_sleep_records (user_id, start_time desc);
create unique index if not exists idx_health_sleep_records_dedupe
  on public.health_sleep_records (user_id, provider_record_name)
  where provider_record_name is not null;

alter table public.health_sleep_records disable row level security;

-- ---------------------------------------------------------------------------
-- 4c. Metric records (heart rate, weight, ...) — "Sample" shaped Google
--     Health data types (a single point-in-time measurement).
-- ---------------------------------------------------------------------------
create table if not exists public.health_metric_records (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  connection_id bigint not null references public.user_health_connections(id) on delete cascade,
  provider text not null default 'google_health',
  provider_record_name text,
  data_type text not null,
  observed_at timestamptz not null,
  start_time timestamptz,
  end_time timestamptz,
  value double precision,
  unit text,
  source_name text,
  raw_metadata jsonb,
  imported_at timestamptz not null default now()
);

create index if not exists idx_health_metric_records_user_type_time
  on public.health_metric_records (user_id, data_type, observed_at desc);
create unique index if not exists idx_health_metric_records_dedupe
  on public.health_metric_records (user_id, data_type, provider_record_name)
  where provider_record_name is not null;

alter table public.health_metric_records disable row level security;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
