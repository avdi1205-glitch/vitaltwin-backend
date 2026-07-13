-- Run this in Supabase SQL Editor to persist VitalTwin users and premium state.
create table if not exists public.vt_users (
  id bigint generated always as identity primary key,
  email text not null unique,
  full_name text not null,
  password text not null,
  premium boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create or replace function public.set_updated_at_vt_users()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_set_updated_at_vt_users on public.vt_users;
create trigger trg_set_updated_at_vt_users
before update on public.vt_users
for each row execute function public.set_updated_at_vt_users();

create table if not exists public.vt_marker_reference (
  marker text primary key,
  lower_bound double precision not null,
  upper_bound double precision not null,
  penalty_below double precision not null default 0,
  penalty_above double precision not null default 0,
  unit text not null default '',
  target_min double precision,
  target_max double precision,
  warn_min double precision,
  warn_max double precision,
  source_name text not null default '',
  source_url text not null default '',
  evidence_level text not null default 'orientierend',
  population_note text not null default 'Erwachsene',
  recommendation text not null,
  updated_at timestamptz not null default now()
);

alter table public.vt_marker_reference add column if not exists unit text not null default '';
alter table public.vt_marker_reference add column if not exists target_min double precision;
alter table public.vt_marker_reference add column if not exists target_max double precision;
alter table public.vt_marker_reference add column if not exists warn_min double precision;
alter table public.vt_marker_reference add column if not exists warn_max double precision;
alter table public.vt_marker_reference add column if not exists source_name text not null default '';
alter table public.vt_marker_reference add column if not exists source_url text not null default '';
alter table public.vt_marker_reference add column if not exists evidence_level text not null default 'orientierend';
alter table public.vt_marker_reference add column if not exists population_note text not null default 'Erwachsene';

insert into public.vt_marker_reference (
  marker,
  lower_bound,
  upper_bound,
  penalty_below,
  penalty_above,
  unit,
  target_min,
  target_max,
  warn_min,
  warn_max,
  source_name,
  source_url,
  evidence_level,
  population_note,
  recommendation
)
values
  ('hba1c', 5.0, 5.6, 0, 2.8, '%', 5.0, 5.6, 4.6, 6.4, 'ADA Standards of Care', 'https://diabetesjournals.org/care/issue/47/Supplement_1', 'hoch', 'Erwachsene ohne Schwangerschaft', 'HbA1c optimieren: Fokus auf stabile Blutzuckerwerte.'),
  ('crp', 0.0, 1.0, 0.0, 3.5, 'mg/L', 0.0, 1.0, 0.0, 3.0, 'AHA/CDC Risikoklassifikation hs-CRP', 'https://www.ahajournals.org', 'mittel', 'Erwachsene, kardiovaskuläre Risikoeinschätzung', 'Entzündungsmanagement verbessern (Ernährung, Schlaf, Stress).'),
  ('vitamin_d', 30.0, 60.0, 0.6, 0.0, 'ng/mL', 30.0, 50.0, 20.0, 80.0, 'Endocrine Society Guideline', 'https://www.endocrine.org', 'mittel', 'Erwachsene, Serum 25(OH)D', 'Vitamin-D-Status regelmäßig kontrollieren und optimieren.'),
  ('apob', 0.0, 90.0, 0.0, 0.4, 'mg/dL', 0.0, 80.0, 0.0, 110.0, 'ESC/EAS Dyslipidämie-Leitlinie', 'https://www.escardio.org', 'hoch', 'Erwachsene, Prävention', 'ApoB senken durch Lebensstil und ärztlich begleitete Strategie.')
on conflict (marker) do update
set
  lower_bound = excluded.lower_bound,
  upper_bound = excluded.upper_bound,
  penalty_below = excluded.penalty_below,
  penalty_above = excluded.penalty_above,
  unit = excluded.unit,
  target_min = excluded.target_min,
  target_max = excluded.target_max,
  warn_min = excluded.warn_min,
  warn_max = excluded.warn_max,
  source_name = excluded.source_name,
  source_url = excluded.source_url,
  evidence_level = excluded.evidence_level,
  population_note = excluded.population_note,
  recommendation = excluded.recommendation,
  updated_at = now();

create table if not exists public.vt_twin_calculations (
  id bigint generated always as identity primary key,
  email text,
  age integer not null,
  gender text not null,
  hba1c double precision not null,
  crp double precision not null,
  vitamin_d double precision not null,
  apob double precision not null,
  biologisches_alter double precision not null,
  differenz double precision not null,
  scenarios jsonb not null,
  marker_breakdown jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_twin_calculations_email_created_at
  on public.vt_twin_calculations (email, created_at desc);

create table if not exists public.vt_user_feedback (
  id bigint generated always as identity primary key,
  email text not null,
  score integer not null,
  message text not null,
  source text not null default 'dashboard',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_user_feedback_email_created_at
  on public.vt_user_feedback (email, created_at desc);

-- Required for backend inserts when using current auth setup.
alter table public.vt_twin_calculations disable row level security;
alter table public.vt_user_feedback disable row level security;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
