-- VitalTwin Enterprise — Founder Operating System, Submodul J: Founder
-- Autopilot.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen.
--
-- Founder Autopilot ist ueberwiegend eine Orchestrierungs-/Aggregations-
-- schicht ueber die bereits bestehenden Submodule A-I (siehe
-- docs/FOUNDER_AUTOPILOT.md) -- nur 5 neue, kleine Tabellen fuer Zustand,
-- Policies, Events, Alerts und Incidents/Kill-Switch. "Daily Plan" und
-- "Weekly Review" werden wie ueberall sonst frisch bei Abruf berechnet,
-- nicht gespeichert.
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.

------------------------------------------------------------------------------
-- 1. Autopilot-Zustand (Modus-Historie; aktueller Zustand = neueste Zeile)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_state (
  id uuid primary key default gen_random_uuid(),
  mode text not null default 'assist',
  kill_switch_active boolean not null default false,
  incident_mode_active boolean not null default false,
  reason text,
  created_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_founder_autopilot_state_created_at on public.vt_founder_autopilot_state(created_at desc);

------------------------------------------------------------------------------
-- 2. Autopilot Policies
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_policies (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  description text not null default '',
  mode text not null default 'assist',
  allowed_categories jsonb not null default '[]'::jsonb,
  blocked_categories jsonb not null default '[]'::jsonb,
  maximum_risk_level text not null default 'low',
  approval_policy text not null default 'always_require_approval',
  financial_threshold numeric,
  execution_window jsonb not null default '{}'::jsonb,
  allowed_environments jsonb not null default '["production"]'::jsonb,
  rollback_required boolean not null default true,
  audit_required boolean not null default true,
  enabled boolean not null default false,
  status text not null default 'entwurf',
  version int not null default 1,
  previous_versions jsonb not null default '[]'::jsonb,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_policies_status on public.vt_founder_autopilot_policies(status);

------------------------------------------------------------------------------
-- 3. Autopilot Events (synthetisiert aus A-I, on-read dedupliziert)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_events (
  id uuid primary key default gen_random_uuid(),
  event_type text not null,
  source_module text not null,
  source_id text,
  severity text not null default 'information',
  occurred_at timestamptz not null default now(),
  payload_reference text,
  dedupe_key text not null unique,
  handled_at timestamptz,
  status text not null default 'offen',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_events_status on public.vt_founder_autopilot_events(status);
create index if not exists idx_vt_founder_autopilot_events_occurred_at on public.vt_founder_autopilot_events(occurred_at desc);

------------------------------------------------------------------------------
-- 4. Autopilot Alerts (dedupliziert, priorisiert, eskalierbar)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_alerts (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  severity text not null default 'mittel',
  title text not null,
  message text not null,
  category text,
  status text not null default 'offen',
  escalated boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_alerts_status on public.vt_founder_autopilot_alerts(status);

------------------------------------------------------------------------------
-- 5. Incidents + Kill-Switch-Ereignisse
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_incidents (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  reason text not null,
  status text not null default 'aktiv',
  activated_by text,
  activated_at timestamptz not null default now(),
  resolved_at timestamptz,
  resolved_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.vt_founder_autopilot_kill_switch_events (
  id uuid primary key default gen_random_uuid(),
  action text not null,
  reason text,
  performed_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_kill_switch_events_created_at on public.vt_founder_autopilot_kill_switch_events(created_at desc);

------------------------------------------------------------------------------
-- 6. Autopilot Queries ("Frag Founder Autopilot")
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_queries (
  id uuid primary key default gen_random_uuid(),
  question text not null,
  answer text,
  insufficient_data boolean not null default false,
  error text,
  created_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_queries_created_at on public.vt_founder_autopilot_queries(created_at desc);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
