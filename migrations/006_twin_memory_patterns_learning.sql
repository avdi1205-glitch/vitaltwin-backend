-- Twin Intelligence Core — Etappe 5: Twin Memory, Pattern Detection und
-- Twin Learning Events.
--
-- STATUS: Entwurf, NICHT gegen eine echte Datenbank ausgeführt (siehe
-- Etappe-2/3/4-Berichte: kein DB-Zugriff in dieser Session verfügbar).
--
-- Non-destruktiv: nur `add column if not exists` auf den bereits in Etappe 2
-- (003_twin_intelligence_foundation.sql) angelegten Tabellen vt_twin_memory /
-- vt_twin_patterns / vt_twin_learning_events. Keine bestehende Spalte wird
-- entfernt oder umbenannt.
--
-- Korrektur einer Etappe-2-Inkonsistenz: die drei Tabellen wurden damals nur
-- mit `user_id not null` angelegt (kein `email`-Feld), obwohl jede andere
-- Twin-Intelligence-Tabelle (vt_wellness_goals, vt_recommendations, ...) seit
-- Etappe 2 konsequent per `email` skopiert wird (die tatsächlich vorhandene,
-- sofort nutzbare Nutzertrennung — `user_id` wird nur mitgeführt, sobald
-- `core/auth.py::get_user_id_by_email` es auflösen kann). Diese Migration
-- gleicht das an: `email` wird ergänzt, `user_id` wird nullable, damit neue
-- Zeilen exakt wie überall sonst zuerst per `email` geschrieben werden
-- können.

------------------------------------------------------------------------------
-- 1. TWIN MEMORY (Etappe 5 §1) --------------------------------------------
------------------------------------------------------------------------------

alter table public.vt_twin_memory
  alter column user_id drop not null;

alter table public.vt_twin_memory
  add column if not exists email text,
  add column if not exists memory_type text,
  add column if not exists title text,
  add column if not exists normalized_value jsonb not null default '{}'::jsonb,
  add column if not exists human_readable_value text,
  add column if not exists source_references jsonb not null default '[]'::jsonb,
  add column if not exists status text not null default 'candidate',
  add column if not exists first_observed_at timestamptz not null default now(),
  add column if not exists last_confirmed_at timestamptz,
  add column if not exists last_used_at timestamptz,
  add column if not exists user_confirmed boolean not null default false,
  add column if not exists deleted_at timestamptz;

-- `memory_key` (existing, Etappe 2) stays as the internal dedup key per
-- user+topic (e.g. "preferred_activity_time"); `active` (existing) is kept
-- for backward compatibility but superseded by the richer `status` column —
-- new code should read/write `status`, not `active`.

create index if not exists idx_vt_twin_memory_email_status
  on public.vt_twin_memory(email, status);

------------------------------------------------------------------------------
-- 2. TWIN PATTERNS (Etappe 5 §3) --------------------------------------------
------------------------------------------------------------------------------

alter table public.vt_twin_patterns
  alter column user_id drop not null;

alter table public.vt_twin_patterns
  add column if not exists email text,
  add column if not exists pattern_key text,
  add column if not exists variables jsonb not null default '[]'::jsonb,
  add column if not exists summary text,
  add column if not exists period_days int,
  add column if not exists data_points int,
  add column if not exists status text not null default 'active',
  add column if not exists contradicting boolean not null default false,
  add column if not exists evidence jsonb not null default '{}'::jsonb,
  add column if not exists updated_at timestamptz not null default now();

create index if not exists idx_vt_twin_patterns_email_status
  on public.vt_twin_patterns(email, status);

------------------------------------------------------------------------------
-- 3. TWIN LEARNING EVENTS (Etappe 5 §4) -------------------------------------
------------------------------------------------------------------------------

alter table public.vt_twin_learning_events
  alter column user_id drop not null;

alter table public.vt_twin_learning_events
  add column if not exists email text,
  add column if not exists source_type text,
  add column if not exists source_id text,
  add column if not exists previous_state jsonb,
  add column if not exists new_state jsonb not null default '{}'::jsonb,
  add column if not exists reason text;

-- `payload` (existing, Etappe 2) stays for backward compatibility; new code
-- writes the more structured `previous_state`/`new_state`/`reason` instead of
-- one big free-text/JSON blob.

create index if not exists idx_vt_twin_learning_events_email_created_at
  on public.vt_twin_learning_events(email, created_at desc);
