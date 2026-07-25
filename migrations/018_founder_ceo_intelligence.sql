-- VitalTwin Enterprise — Founder Operating System, Submodul H: CEO
-- Intelligence.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Bewusst NUR 2 neue Tabellen: CEO Intelligence ist ueberwiegend eine
-- Aggregations-/Syntheseschicht ueber die bereits bestehenden Founder-OS-
-- Tabellen (vt_founder_business_goals, vt_founder_business_insights,
-- vt_founder_tasks, vt_founder_approvals, vt_automation_*) -- siehe
-- docs/CEO_INTELLIGENCE.md fuer die vollstaendige Begruendung, warum
-- KEIN ExecutiveGoal/ExecutiveInsight/ExecutiveRisk/ExecutiveOpportunity/
-- ExecutiveScorecard/ExecutiveSummary als eigene Tabelle angelegt wird.
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.

------------------------------------------------------------------------------
-- 1. ExecutiveScenario (gespeicherte Was-waere-wenn-Simulationen)
------------------------------------------------------------------------------

create table if not exists public.vt_executive_scenarios (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  scenario_type text not null,
  assumptions jsonb not null default '{}'::jsonb,
  results jsonb not null default '{}'::jsonb,
  computable boolean not null default true,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_executive_scenarios_created_at on public.vt_executive_scenarios(created_at desc);

------------------------------------------------------------------------------
-- 2. ExecutiveQuery ("Frag CEO Intelligence")
------------------------------------------------------------------------------

create table if not exists public.vt_executive_queries (
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

create index if not exists idx_vt_executive_queries_created_at on public.vt_executive_queries(created_at desc);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
