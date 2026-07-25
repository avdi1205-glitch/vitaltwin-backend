-- VitalTwin Enterprise — Founder Operating System, Submodul E: AI
-- Business Coach.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`.
--
-- WICHTIG (Modul-Abgrenzung): Diese Tabellen gehoeren ausschliesslich zum
-- Founder Operating System. Sie speichern NIEMALS individuelle Wellness-,
-- CGM-, Nutrition-, Schlaf-, Bewegungs- oder Twin-Memory-Daten -- nur
-- aggregierte Geschaeftskennzahlen und Meta-Text (Titel, Begruendungen).
--
-- Bewusst NICHT angelegt (siehe docs/AI_BUSINESS_COACH.md §Datenmodell):
-- FounderBusinessRisk / FounderBusinessOpportunity als eigene Tabellen
-- (Chancen/Risiken sind Insights, gefiltert nach `category`) und
-- FounderMetricSnapshot (Kennzahlen werden bei jedem Aufruf frisch aus
-- den bereits zeitgestempelten Rohdaten berechnet, keine Zwischenspeicherung
-- noetig) -- um keine doppelte, ueberlappende Infrastruktur zu bauen.

------------------------------------------------------------------------------
-- 1. FounderBusinessGoal
------------------------------------------------------------------------------

create table if not exists public.vt_founder_business_goals (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  category text not null,
  start_value numeric,
  target_value numeric not null,
  start_date date,
  target_date date,
  status text not null default 'geplant',
  current_progress numeric,
  data_source text not null default 'nicht verbunden',
  responsible_module text,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_business_goals_status on public.vt_founder_business_goals(status);
create index if not exists idx_vt_founder_business_goals_category on public.vt_founder_business_goals(category);

------------------------------------------------------------------------------
-- 2. FounderBusinessInsight (deckt auch "Chancen" und "Risiken" ab --
--    per category-Filter, keine separaten Tabellen)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_business_insights (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  title text not null,
  category text not null,
  description text not null,
  data_basis text not null,
  period_start date,
  period_end date,
  comparison_period_start date,
  comparison_period_end date,
  severity text not null default 'mittel',
  confidence text not null default 'mittel',
  possible_cause text,
  possible_impact text,
  recommended_action text,
  estimated_effort text,
  expected_benefit text,
  status text not null default 'erkannt',
  source text not null default 'regelbasiert',
  source_references jsonb not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_business_insights_category on public.vt_founder_business_insights(category);
create index if not exists idx_vt_founder_business_insights_status on public.vt_founder_business_insights(status);
create index if not exists idx_vt_founder_business_insights_severity on public.vt_founder_business_insights(severity);

------------------------------------------------------------------------------
-- 3. FounderBusinessRecommendation
------------------------------------------------------------------------------

create table if not exists public.vt_founder_business_recommendations (
  id uuid primary key default gen_random_uuid(),
  insight_id uuid references public.vt_founder_business_insights(id) on delete set null,
  title text not null,
  reasoning text not null,
  data_basis text not null,
  expected_benefit text,
  risk text,
  effort text,
  priority text not null default 'mittel',
  success_metric text,
  test_period text,
  status text not null default 'offen',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_business_recommendations_status on public.vt_founder_business_recommendations(status);

------------------------------------------------------------------------------
-- 4. FounderCoachQuery + FounderCoachResponse (eine Tabelle: Frage+Antwort
--    gehoeren untrennbar zusammen)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_coach_queries (
  id uuid primary key default gen_random_uuid(),
  question text not null,
  answer text,
  insufficient_data boolean not null default false,
  ai_provider text,
  latency_ms int,
  error text,
  created_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_coach_queries_created_at on public.vt_founder_coach_queries(created_at desc);

------------------------------------------------------------------------------
-- 5. FounderAutomationEvent (fuer den Automation Score)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_automation_events (
  id uuid primary key default gen_random_uuid(),
  event_type text not null,
  reference_table text,
  reference_id text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_automation_events_type on public.vt_founder_automation_events(event_type);
create index if not exists idx_vt_founder_automation_events_created_at on public.vt_founder_automation_events(created_at desc);
