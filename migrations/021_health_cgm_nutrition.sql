-- ============================================================================
-- Migration 021: Health Data — CGM (Continuous Glucose Monitor) Import &
-- Manual Nutrition Logging.
-- ============================================================================
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen.
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`. Keine bestehende Tabelle wird veraendert,
-- umbenannt oder geloescht.
--
-- Erste echte Speicherung individueller CGM-/Ernaehrungsdaten in dieser
-- Codebase (siehe app/routers/health.py). Beide Tabellen sind ausschliesslich
-- per `email` skaliert -- jeder Endpunkt liest/schreibt nur Zeilen des
-- authentifizierten Nutzers (core/auth.py::require_email), niemals anhand
-- einer vom Client mitgesendeten ID.

create table if not exists public.vt_cgm_readings (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  glucose_value numeric not null,
  reading_at timestamptz not null,
  source text not null default 'unknown',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_cgm_readings_email_reading_at
  on public.vt_cgm_readings(email, reading_at desc);

create table if not exists public.vt_nutrition_entries (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  meal_name text not null,
  carbs numeric not null default 0,
  protein numeric not null default 0,
  fat numeric not null default 0,
  calories numeric not null default 0,
  logged_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_nutrition_entries_email_logged_at
  on public.vt_nutrition_entries(email, logged_at desc);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
