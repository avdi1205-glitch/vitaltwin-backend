-- VitalTwin Enterprise — Founder Operating System, Submodul G: Automation
-- Engine.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` / `create index if not
-- exists` / additive `alter table ... add column if not exists`.
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.
-- Nur Founder-OS-Automationsregeln, deren Ausfuehrungshistorie und
-- abgeleitete, aggregierte Prozessdaten.
--
-- Kein Hintergrund-Scheduler/Queue existiert in dieser Codebase (Railway,
-- Single-Prozess, keine Celery/Redis-Queue). "Zeitgesteuerte" Regeln
-- werden -- konsistent mit jedem anderen Founder-OS-Submodul -- beim
-- Laden des Dashboards ODER durch einen expliziten Aufruf von
-- `POST /api/admin/founder/automation/run-due` ausgewertet (z. B. durch
-- einen externen Cron-Aufruf). Es gibt keine serverseitige Dauerschleife.

------------------------------------------------------------------------------
-- 1. AutomationRule (+ Versionierung)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_rules (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  description text not null default '',
  category text not null,
  trigger_type text not null,
  trigger_config jsonb not null default '{}'::jsonb,
  conditions jsonb not null default '[]'::jsonb,
  actions jsonb not null default '[]'::jsonb,
  risk_level text not null,
  approval_policy text not null default 'no_approval',
  retry_policy jsonb not null default '{"type": "none", "max_attempts": 1, "cooldown_seconds": 60}'::jsonb,
  timeout_seconds int not null default 30,
  max_runs int,
  run_count int not null default 0,
  enabled boolean not null default false,
  status text not null default 'entwurf',
  environment text not null default 'production',
  rollout_stage text not null default 'nur_founder',
  approved_once boolean not null default false,
  version int not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  created_by text,
  last_run_at timestamptz,
  next_run_at timestamptz
);

create index if not exists idx_vt_automation_rules_status on public.vt_automation_rules(status);
create index if not exists idx_vt_automation_rules_category on public.vt_automation_rules(category);
create index if not exists idx_vt_automation_rules_enabled on public.vt_automation_rules(enabled);
create index if not exists idx_vt_automation_rules_next_run_at on public.vt_automation_rules(next_run_at);

create table if not exists public.vt_automation_rule_versions (
  id uuid primary key default gen_random_uuid(),
  rule_id uuid not null references public.vt_automation_rules(id) on delete cascade,
  version int not null,
  snapshot jsonb not null,
  created_at timestamptz not null default now(),
  created_by text,
  unique (rule_id, version)
);

create index if not exists idx_vt_automation_rule_versions_rule_id on public.vt_automation_rule_versions(rule_id);

------------------------------------------------------------------------------
-- 2. AutomationRun (inkl. Steps, Retry, Rollback)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_runs (
  id uuid primary key default gen_random_uuid(),
  rule_id uuid references public.vt_automation_rules(id) on delete set null,
  idempotency_key text not null unique,
  trigger_type text not null,
  trigger_signature text,
  status text not null default 'wartend',
  risk_level text,
  environment text,
  attempt int not null default 1,
  max_attempts int not null default 1,
  dry_run boolean not null default false,
  steps jsonb not null default '[]'::jsonb,
  result jsonb,
  error text,
  previous_state jsonb,
  rollback_status text,
  rollback_at timestamptz,
  rollback_by text,
  approval_id text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_runs_rule_id on public.vt_automation_runs(rule_id);
create index if not exists idx_vt_automation_runs_status on public.vt_automation_runs(status);
create index if not exists idx_vt_automation_runs_created_at on public.vt_automation_runs(created_at desc);

------------------------------------------------------------------------------
-- 3. AutomationDeadLetter
------------------------------------------------------------------------------

create table if not exists public.vt_automation_dead_letters (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references public.vt_automation_runs(id) on delete set null,
  rule_id uuid references public.vt_automation_rules(id) on delete set null,
  reason text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_dead_letters_rule_id on public.vt_automation_dead_letters(rule_id);

------------------------------------------------------------------------------
-- 4. AutomationOpportunity (Vorschlaege, nie automatisch aktiviert)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_opportunities (
  id uuid primary key default gen_random_uuid(),
  signature text not null unique,
  category text,
  description text not null,
  occurrences int not null default 1,
  source_table text not null,
  suggested_rule jsonb,
  status text not null default 'neu',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_opportunities_status on public.vt_automation_opportunities(status);

------------------------------------------------------------------------------
-- 5. AutomationAlert (dedupliziert, priorisiert)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_alerts (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  severity text not null default 'mittel',
  title text not null,
  message text not null,
  category text,
  source_run_id uuid references public.vt_automation_runs(id) on delete set null,
  status text not null default 'offen',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_alerts_status on public.vt_automation_alerts(status);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
