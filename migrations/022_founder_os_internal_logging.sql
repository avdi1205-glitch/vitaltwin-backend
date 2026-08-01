-- Founder OS internal foundations: AI usage logging (tokens/cost), system
-- events, error events, releases, backup status. All tables start EMPTY —
-- no demo data, no backfilled/fabricated rows. Run once in Supabase SQL
-- Editor, then `notify pgrst, 'reload schema'` at the end refreshes
-- PostgREST's cache so the new tables/columns are usable immediately.

-- ---------------------------------------------------------------------------
-- 1. Central AI usage log — every AI request (twin chat + all Founder-OS
--    "Ask" endpoints) writes exactly one row here, success or failure.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_ai_usage_events (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  email text,
  feature text not null,
  model text,
  status text not null default 'success',
  error_type text,
  prompt_tokens integer,
  completion_tokens integer,
  total_tokens integer,
  cost_usd double precision,
  cost_note text,
  latency_ms integer
);

create index if not exists idx_vt_ai_usage_events_created_at
  on public.vt_ai_usage_events (created_at desc);
create index if not exists idx_vt_ai_usage_events_feature
  on public.vt_ai_usage_events (feature);

alter table public.vt_ai_usage_events disable row level security;

-- ---------------------------------------------------------------------------
-- 2. Central system event log — generic lifecycle/operational events
--    (startup, unhandled exceptions, future deploy hooks, ...).
-- ---------------------------------------------------------------------------
create table if not exists public.vt_system_events (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  event_type text not null,
  severity text not null default 'info',
  source text,
  message text not null,
  metadata jsonb
);

create index if not exists idx_vt_system_events_created_at
  on public.vt_system_events (created_at desc);

alter table public.vt_system_events disable row level security;

-- ---------------------------------------------------------------------------
-- 3. Dedicated error event log — every unhandled backend exception, kept
--    separate from the generic system event log for easy error-rate queries.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_error_events (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  source text not null,
  error_type text not null,
  message text not null,
  request_path text,
  email text
);

create index if not exists idx_vt_error_events_created_at
  on public.vt_error_events (created_at desc);

alter table public.vt_error_events disable row level security;

-- ---------------------------------------------------------------------------
-- 4. Releases — real data model for "letzter Release", populated by a
--    founder/admin (or later a CI/CD webhook) via POST /api/admin/system/releases.
--    Empty until the first real entry — never auto-fabricated.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_founder_releases (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  version text not null,
  git_commit_sha text,
  environment text not null default 'production',
  description text,
  released_by text,
  released_at timestamptz not null default now(),
  build_status text not null default 'unbekannt'
);

create index if not exists idx_vt_founder_releases_released_at
  on public.vt_founder_releases (released_at desc);

alter table public.vt_founder_releases disable row level security;

-- ---------------------------------------------------------------------------
-- 5. Backup status — real data model for "letzter Backup-Status", populated
--    by a founder/admin (or later a real backup job) via
--    POST /api/admin/system/backups. Empty until the first real entry.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_founder_backup_status (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  backup_type text not null default 'database',
  status text not null,
  size_bytes bigint,
  completed_at timestamptz,
  note text,
  recorded_by text
);

create index if not exists idx_vt_founder_backup_status_completed_at
  on public.vt_founder_backup_status (completed_at desc);

alter table public.vt_founder_backup_status disable row level security;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
