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
  recommendation text not null,
  updated_at timestamptz not null default now()
);

insert into public.vt_marker_reference (marker, lower_bound, upper_bound, penalty_below, penalty_above, recommendation)
values
  ('hba1c', 5.0, 5.0, 0, 2.8, 'HbA1c optimieren: Fokus auf stabile Blutzuckerwerte.'),
  ('crp', 1.0, 1.0, 0, 3.5, 'Entzuendungsmanagement verbessern (Ernaehrung, Schlaf, Stress).'),
  ('vitamin_d', 40.0, 120.0, 0.6, 0, 'Vitamin-D-Status regelmaessig kontrollieren und optimieren.'),
  ('apob', 70.0, 70.0, 0, 0.4, 'ApoB senken durch Lebensstil und aerztlich begleitete Strategie.')
on conflict (marker) do update
set
  lower_bound = excluded.lower_bound,
  upper_bound = excluded.upper_bound,
  penalty_below = excluded.penalty_below,
  penalty_above = excluded.penalty_above,
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

-- Required for backend inserts when using current auth setup.
alter table public.vt_twin_calculations disable row level security;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
