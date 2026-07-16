-- Block 5: Personal wellness profile foundation.
-- Non-destructive: only creates new tables/indexes. Does not alter or drop
-- any existing table (vt_users, vt_user_feedback, vt_twin_calculations,
-- vt_marker_reference, vt_beta_applications are all untouched).
-- Run this once in the Supabase SQL editor.

create table if not exists public.vt_user_profiles (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  display_name text,
  birth_year int,
  age_group text,
  gender text,
  height_cm numeric,
  weight_kg numeric,
  preferred_language text not null default 'de',
  timezone text not null default 'Europe/Berlin',
  unit_system text not null default 'metric',
  wellness_goals text[] not null default '{}',
  onboarding_completed boolean not null default false,
  deletion_requested_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.vt_daily_wellness_entries (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  entry_date date not null,
  sleep_hours numeric,
  movement_days_per_week int,
  steps int,
  stress_level int,
  energy_level int,
  nutrition_habit text,
  water_habit text,
  created_at timestamptz not null default now(),
  unique (email, entry_date)
);

create table if not exists public.vt_habits (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  name text not null,
  category text not null,
  frequency text not null,
  target text,
  reminder_enabled boolean not null default false,
  reminder_time text,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.vt_habit_entries (
  id uuid primary key default gen_random_uuid(),
  habit_id uuid not null references public.vt_habits(id) on delete cascade,
  email text not null,
  entry_date date not null,
  completed boolean not null default true,
  created_at timestamptz not null default now(),
  unique (habit_id, entry_date)
);

create index if not exists idx_vt_daily_wellness_entries_email on public.vt_daily_wellness_entries(email);
create index if not exists idx_vt_habits_email on public.vt_habits(email);
create index if not exists idx_vt_habit_entries_habit_id on public.vt_habit_entries(habit_id);
create index if not exists idx_vt_habit_entries_email on public.vt_habit_entries(email);
