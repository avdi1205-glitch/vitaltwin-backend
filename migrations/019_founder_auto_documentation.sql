-- VitalTwin Enterprise — Founder Operating System, Submodul I: Auto
-- Documentation.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Wichtige Architektur-Entscheidung (siehe docs/AUTO_DOCUMENTATION.md):
-- Dieses Submodul schreibt NIEMALS auf das Dateisystem (weder Backend-
-- noch Frontend-Repository). "Generierte Dokumentation" wird
-- ausschliesslich als Text in `generated_content`/`vt_documentation_
-- versions` gespeichert -- der Gruender kopiert daraus manuell in echte
-- .md-Dateien, falls gewuenscht. Das eliminiert jedes Path-Traversal-/
-- Ueberschreibungs-Risiko vollstaendig (kein Schreibpfad existiert).
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.

------------------------------------------------------------------------------
-- 1. DocumentationRegistry (+ Versionierung)
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_registry (
  id uuid primary key default gen_random_uuid(),
  document_path text not null unique,
  title text not null,
  category text not null,
  module text not null default 'founder_os',
  submodule text,
  owner text,
  status text not null default 'draft',
  source_files jsonb not null default '[]'::jsonb,
  last_generated_at timestamptz,
  last_reviewed_at timestamptz,
  last_approved_at timestamptz,
  version int not null default 1,
  content_hash text,
  source_hash text,
  is_generated boolean not null default false,
  requires_approval boolean not null default false,
  protected boolean not null default false,
  stale_reason text,
  generated_content text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_documentation_registry_status on public.vt_documentation_registry(status);
create index if not exists idx_vt_documentation_registry_category on public.vt_documentation_registry(category);
create index if not exists idx_vt_documentation_registry_module on public.vt_documentation_registry(module);

create table if not exists public.vt_documentation_versions (
  id uuid primary key default gen_random_uuid(),
  registry_id uuid not null references public.vt_documentation_registry(id) on delete cascade,
  version int not null,
  content text,
  content_hash text,
  diff_summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  created_by text,
  unique (registry_id, version)
);

create index if not exists idx_vt_documentation_versions_registry_id on public.vt_documentation_versions(registry_id);

------------------------------------------------------------------------------
-- 2. DocumentationGenerationRun
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_generation_runs (
  id uuid primary key default gen_random_uuid(),
  run_type text not null,
  status text not null default 'laeuft',
  items_scanned int not null default 0,
  items_updated int not null default 0,
  items_flagged_stale int not null default 0,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_documentation_generation_runs_created_at on public.vt_documentation_generation_runs(created_at desc);

------------------------------------------------------------------------------
-- 3. DocumentationChangeProposal (fuer geschuetzte Dokumente)
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_change_proposals (
  id uuid primary key default gen_random_uuid(),
  registry_id uuid references public.vt_documentation_registry(id) on delete set null,
  proposed_content text,
  diff_summary jsonb not null default '{}'::jsonb,
  reason text,
  risk_level text not null default 'mittel',
  status text not null default 'offen',
  approval_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_documentation_change_proposals_status on public.vt_documentation_change_proposals(status);

------------------------------------------------------------------------------
-- 4. DocumentationQuery ("Frag die Projektdokumentation")
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_queries (
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

create index if not exists idx_vt_documentation_queries_created_at on public.vt_documentation_queries(created_at desc);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
