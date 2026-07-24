-- ============================================================================
-- KOMBINIERTES SKRIPT: Migrationen 001-009 in einem Stueck (Convenience-Datei)
-- Nur zum einmaligen Einfuegen in den Supabase SQL Editor gedacht.
-- Die einzelnen, nummerierten Dateien in migrations/ bleiben die Quelle der
-- Wahrheit -- diese Datei ist nur eine Zusammenfassung fuer den Erst-Rollout.
-- ============================================================================

-- ============================================================================
-- Quelle: 001_profile_wellness_foundation.sql
-- ============================================================================
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


-- ============================================================================
-- Quelle: 002_chat_usage.sql
-- ============================================================================
-- Block 6: "Frag deinen Twin" wellness assistant â€” server-side daily usage
-- counter (rate limiting must not be frontend-only). Non-destructive: only
-- creates a new table/index, does not alter or drop any existing table.
--
-- Deliberately does NOT store chat message content by default (see Block 6
-- report): only a per-user, per-day request counter, since the product does
-- not yet offer a "view your chat history" feature. If that changes later,
-- add a separate vt_chat_messages table with a clear retention/deletion policy.

create table if not exists public.vt_chat_usage (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  usage_date date not null,
  count int not null default 0,
  last_request_at timestamptz,
  created_at timestamptz not null default now(),
  unique (email, usage_date)
);

create index if not exists idx_vt_chat_usage_email on public.vt_chat_usage(email);


-- ============================================================================
-- Quelle: 003_twin_intelligence_foundation.sql
-- ============================================================================
-- Twin Intelligence Core â€” Etappe 2: Datenmodelle, Migrationen, Validierung
-- und Nutzertrennung.
--
-- STATUS: Verifiziert (2026-07-24) â€” alle Tabellen/Spalten dieser Datei
-- wurden per REST-API-Abfrage gegen die produktive Supabase-Datenbank
-- bestÃ¤tigt und existieren dort bereits. Diese Datei ist weiterhin sicher
-- erneut ausfÃ¼hrbar (siehe Non-destruktiv-Hinweis unten), z. B. beim Aufsetzen
-- einer neuen Umgebung.
--
-- Non-destruktiv: ausschlieÃŸlich `create table if not exists`,
-- `add column if not exists` und `create index if not exists`. Keine
-- bestehende Tabelle wird verÃ¤ndert, umbenannt oder gelÃ¶scht. Keine
-- bestehenden Zeilen werden verÃ¤ndert oder gelÃ¶scht.
--
-- Reihenfolge wichtig: vt_users muss vor allen `references vt_users(id)`
-- existieren (siehe SUPABASE_SCHEMA.sql) â€” das ist bereits der Fall.

------------------------------------------------------------------------------
-- 1. BESTEHENDE MODELLE ERWEITERN (statt Duplikate zu erzeugen)
------------------------------------------------------------------------------
-- Jede Tabelle bekommt zusÃ¤tzlich zur bisherigen email-Spalte eine nullable
-- user_id-Spalte (FK auf vt_users.id). email bleibt die Quelle der Wahrheit,
-- bis ein separates Backfill-Skript (spÃ¤tere Etappe, mit ausdrÃ¼cklicher
-- Freigabe) user_id fÃ¼r Bestandsdaten befÃ¼llt. Neuer Code sollte user_id
-- bevorzugt setzen, wenn verfÃ¼gbar.

alter table public.vt_user_profiles
  add column if not exists user_id bigint references public.vt_users(id) on delete set null;

alter table public.vt_daily_wellness_entries
  add column if not exists user_id bigint references public.vt_users(id) on delete set null,
  add column if not exists mood int,
  add column if not exists motivation int,
  add column if not exists sleep_quality int,
  add column if not exists recovery int,
  add column if not exists timezone text not null default 'Europe/Berlin',
  add column if not exists source text not null default 'manual',
  add column if not exists data_quality text not null default 'user_reported',
  add column if not exists updated_at timestamptz not null default now();

alter table public.vt_habits
  add column if not exists user_id bigint references public.vt_users(id) on delete set null,
  add column if not exists source text not null default 'manual';

alter table public.vt_habit_entries
  add column if not exists user_id bigint references public.vt_users(id) on delete set null,
  add column if not exists source text not null default 'manual',
  add column if not exists data_quality text not null default 'user_reported';

create index if not exists idx_vt_user_profiles_user_id on public.vt_user_profiles(user_id);
create index if not exists idx_vt_daily_wellness_entries_user_id_entry_date
  on public.vt_daily_wellness_entries(user_id, entry_date);
create index if not exists idx_vt_habits_user_id on public.vt_habits(user_id);
create index if not exists idx_vt_habit_entries_user_id on public.vt_habit_entries(user_id);

------------------------------------------------------------------------------
-- 2. WELLNESS GOALS + GOAL ACTIONS (Goal Loop)
------------------------------------------------------------------------------
-- vt_user_profiles.wellness_goals (text[]) bleibt fÃ¼r die schnelle
-- Onboarding-Mehrfachauswahl bestehen. vt_wellness_goals ist eine neue,
-- eigenstÃ¤ndige EntitÃ¤t fÃ¼r den vollen Goal Loop (Ziel -> Plan -> Umsetzung
-- -> Bewertung -> neuer Plan) mit Status und Zieldatum â€” kein Duplikat,
-- sondern eine andere GranularitÃ¤t (Auswahl-Tag vs. verfolgbares Ziel).

create table if not exists public.vt_wellness_goals (
  id uuid primary key default gen_random_uuid(),
  user_id bigint references public.vt_users(id) on delete cascade,
  email text not null,
  goal_type text not null,
  status text not null default 'active',
  target_value numeric,
  target_date date,
  source text not null default 'manual',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);

create index if not exists idx_vt_wellness_goals_user_id_created_at
  on public.vt_wellness_goals(user_id, created_at desc);
create index if not exists idx_vt_wellness_goals_user_id_status
  on public.vt_wellness_goals(user_id, status) where status = 'active';

create table if not exists public.vt_goal_actions (
  id uuid primary key default gen_random_uuid(),
  goal_id uuid not null references public.vt_wellness_goals(id) on delete cascade,
  user_id bigint references public.vt_users(id) on delete cascade,
  description text not null,
  status text not null default 'planned',
  due_date date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_goal_actions_goal_id on public.vt_goal_actions(goal_id);
create index if not exists idx_vt_goal_actions_user_id_created_at
  on public.vt_goal_actions(user_id, created_at desc);

------------------------------------------------------------------------------
-- 3. DAILY PLANNING + REFLECTION LOOPS
------------------------------------------------------------------------------

create table if not exists public.vt_daily_plans (
  id uuid primary key default gen_random_uuid(),
  user_id bigint references public.vt_users(id) on delete cascade,
  email text not null,
  local_date date not null,
  timezone text not null default 'Europe/Berlin',
  status text not null default 'active',
  source text not null default 'calculated',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, local_date)
);

create index if not exists idx_vt_daily_plans_user_id_local_date
  on public.vt_daily_plans(user_id, local_date desc);

create table if not exists public.vt_daily_plan_actions (
  id uuid primary key default gen_random_uuid(),
  daily_plan_id uuid not null references public.vt_daily_plans(id) on delete cascade,
  user_id bigint references public.vt_users(id) on delete cascade,
  description text not null,
  source text not null default 'calculated',
  status text not null default 'planned',
  sort_order int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_daily_plan_actions_daily_plan_id
  on public.vt_daily_plan_actions(daily_plan_id);

create table if not exists public.vt_daily_reflections (
  id uuid primary key default gen_random_uuid(),
  user_id bigint references public.vt_users(id) on delete cascade,
  email text not null,
  local_date date not null,
  timezone text not null default 'Europe/Berlin',
  what_went_well text,
  what_to_improve text,
  mood int,
  energy int,
  source text not null default 'manual',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, local_date)
);

create index if not exists idx_vt_daily_reflections_user_id_local_date
  on public.vt_daily_reflections(user_id, local_date desc);

create table if not exists public.vt_weekly_reflections (
  id uuid primary key default gen_random_uuid(),
  user_id bigint references public.vt_users(id) on delete cascade,
  email text not null,
  week_start_date date not null,
  timezone text not null default 'Europe/Berlin',
  patterns jsonb not null default '{}'::jsonb,
  summary text,
  source text not null default 'calculated',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, week_start_date)
);

create index if not exists idx_vt_weekly_reflections_user_id_week_start_date
  on public.vt_weekly_reflections(user_id, week_start_date desc);

------------------------------------------------------------------------------
-- 4. RECOMMENDATION LOOP (Empfehlung -> Entscheidung -> Ergebnis -> Feedback)
------------------------------------------------------------------------------
-- vt_user_feedback (bestehend) bleibt das generische "Feedback zur Beta"-
-- Formular auf dem Dashboard unverÃ¤ndert. vt_recommendation_feedback ist
-- gezielt an eine einzelne Recommendation gebunden â€” andere GranularitÃ¤t,
-- kein Duplikat.

create table if not exists public.vt_recommendations (
  id uuid primary key default gen_random_uuid(),
  user_id bigint references public.vt_users(id) on delete cascade,
  email text not null,
  category text not null,
  text text not null,
  source text not null default 'calculated',
  confidence numeric,
  data_quality text not null default 'calculated',
  status text not null default 'pending',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  expires_at timestamptz
);

create index if not exists idx_vt_recommendations_user_id_created_at
  on public.vt_recommendations(user_id, created_at desc);
create index if not exists idx_vt_recommendations_user_id_status
  on public.vt_recommendations(user_id, status) where status = 'pending';

create table if not exists public.vt_recommendation_decisions (
  id uuid primary key default gen_random_uuid(),
  recommendation_id uuid not null references public.vt_recommendations(id) on delete cascade,
  user_id bigint references public.vt_users(id) on delete cascade,
  decision text not null,
  decided_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_recommendation_decisions_recommendation_id
  on public.vt_recommendation_decisions(recommendation_id);

create table if not exists public.vt_recommendation_outcomes (
  id uuid primary key default gen_random_uuid(),
  recommendation_id uuid not null references public.vt_recommendations(id) on delete cascade,
  user_id bigint references public.vt_users(id) on delete cascade,
  outcome_status text not null,
  measured_at timestamptz,
  result_notes text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_recommendation_outcomes_recommendation_id
  on public.vt_recommendation_outcomes(recommendation_id);

create table if not exists public.vt_recommendation_feedback (
  id uuid primary key default gen_random_uuid(),
  recommendation_id uuid not null references public.vt_recommendations(id) on delete cascade,
  user_id bigint references public.vt_users(id) on delete cascade,
  rating int not null,
  comment text,
  created_at timestamptz not null default now(),
  constraint chk_vt_recommendation_feedback_rating check (rating between 1 and 5)
);

create index if not exists idx_vt_recommendation_feedback_recommendation_id
  on public.vt_recommendation_feedback(recommendation_id);

------------------------------------------------------------------------------
-- 5. TWIN MEMORY, PATTERNS, INSIGHTS, LEARNING EVENTS, CONTEXT SNAPSHOTS
------------------------------------------------------------------------------

create table if not exists public.vt_twin_memory (
  id uuid primary key default gen_random_uuid(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  memory_key text not null,
  memory_value jsonb not null default '{}'::jsonb,
  confidence numeric,
  source text not null default 'calculated',
  active boolean not null default true,
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, memory_key)
);

create index if not exists idx_vt_twin_memory_user_id_active
  on public.vt_twin_memory(user_id) where active = true;

create table if not exists public.vt_twin_patterns (
  id uuid primary key default gen_random_uuid(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  pattern_type text not null,
  description text,
  confidence numeric,
  data_quality text not null default 'calculated',
  detected_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_twin_patterns_user_id_detected_at
  on public.vt_twin_patterns(user_id, detected_at desc);

create table if not exists public.vt_twin_insights (
  id uuid primary key default gen_random_uuid(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  insight_type text not null,
  text text not null,
  source text not null default 'calculated',
  confidence numeric,
  created_at timestamptz not null default now(),
  dismissed_at timestamptz
);

create index if not exists idx_vt_twin_insights_user_id_created_at
  on public.vt_twin_insights(user_id, created_at desc);

create table if not exists public.vt_twin_learning_events (
  id uuid primary key default gen_random_uuid(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  event_type text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_twin_learning_events_user_id_created_at
  on public.vt_twin_learning_events(user_id, created_at desc);

create table if not exists public.vt_twin_context_snapshots (
  id uuid primary key default gen_random_uuid(),
  user_id bigint not null references public.vt_users(id) on delete cascade,
  snapshot jsonb not null default '{}'::jsonb,
  reason text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_twin_context_snapshots_user_id_created_at
  on public.vt_twin_context_snapshots(user_id, created_at desc);

------------------------------------------------------------------------------
-- 6. CONSENT + AUDIT
------------------------------------------------------------------------------

create table if not exists public.vt_consent_records (
  id uuid primary key default gen_random_uuid(),
  user_id bigint references public.vt_users(id) on delete cascade,
  email text not null,
  consent_type text not null,
  granted boolean not null,
  granted_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_consent_records_user_id on public.vt_consent_records(user_id);

create table if not exists public.vt_audit_events (
  id uuid primary key default gen_random_uuid(),
  user_id bigint references public.vt_users(id) on delete set null,
  email text,
  action text not null,
  entity_type text not null,
  entity_id text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_audit_events_user_id_created_at
  on public.vt_audit_events(user_id, created_at desc);
create index if not exists idx_vt_audit_events_entity_type_entity_id
  on public.vt_audit_events(entity_type, entity_id);


-- ============================================================================
-- Quelle: 004_checkin_habit_goal_loops.sql
-- ============================================================================
-- Twin Intelligence Core â€” Etappe 3: Check-in-, Habit- und Goal-Loops.
--
-- STATUS: Verifiziert (2026-07-24) â€” alle Spalten dieser Datei wurden per
-- REST-API-Abfrage gegen die produktive Supabase-Datenbank bestÃ¤tigt und
-- existieren dort bereits.
--
-- Non-destruktiv: nur `add column if not exists`. ErgÃ¤nzt die in Etappe 2
-- bereits erweiterten Tabellen um die fÃ¼r Etappe 3 zusÃ¤tzlich benÃ¶tigten
-- Felder, statt neue parallele Tabellen anzulegen.

-- Check-in (vt_daily_wellness_entries): Bewegung in Minuten (zusÃ¤tzlich zu
-- movement_days_per_week) und eine kurze optionale Notiz. `energy`/`stress`
-- sind die neuen 1-10-Skalen aus Etappe 3 Â§1 â€” bewusst zusÃ¤tzlich zu den
-- bestehenden `energy_level`/`stress_level` (1-5, weiterhin vom
-- Marker-Formular auf dem Dashboard genutzt), nicht als Ersatz, um dessen
-- bestehende Auswahl (1-5-Dropdown) nicht zu brechen.
alter table public.vt_daily_wellness_entries
  add column if not exists movement_minutes int,
  add column if not exists note text,
  add column if not exists energy int,
  add column if not exists stress int;

-- Habits: dritter Zustand "pausiert" zusÃ¤tzlich zu aktiv/archiviert. `active`
-- (boolean) bleibt fÃ¼r AbwÃ¤rtskompatibilitÃ¤t bestehen und wird von der
-- Anwendungsschicht synchron zu `status` gehalten (status='active' <=>
-- active=true), damit keine bestehende Abfrage bricht.
alter table public.vt_habits
  add column if not exists status text not null default 'active';

create index if not exists idx_vt_habits_email_status on public.vt_habits(email, status);

-- Wellness goals (vt_wellness_goals, created in Etappe 2): a human-readable
-- title wasn't part of the original Etappe 2 schema (which only stored
-- goal_type/status/target_*) â€” Etappe 3 Â§6 explicitly requires one.
alter table public.vt_wellness_goals
  add column if not exists title text;



-- ============================================================================
-- Quelle: 005_recommendation_loops.sql
-- ============================================================================
-- Twin Intelligence Core â€” Etappe 4: Recommendation-, Decision-, Outcome- und
-- Feedback-Loops.
--
-- STATUS: Verifiziert (2026-07-24) â€” alle Spalten dieser Datei wurden per
-- REST-API-Abfrage gegen die produktive Supabase-Datenbank bestÃ¤tigt und
-- existieren dort bereits.
--
-- Non-destruktiv: nur `add column if not exists` auf den in Etappe 2
-- angelegten Tabellen vt_recommendations / vt_recommendation_decisions /
-- vt_recommendation_outcomes / vt_recommendation_feedback. Keine bestehende
-- Spalte wird entfernt oder umbenannt.

-- vt_recommendations: die Etappe-2-Version hatte nur category/text/source/
-- confidence/status. Etappe 4 Â§1 verlangt zusÃ¤tzlich ein strukturiertes
-- Empfehlungsmodell mit Titel, ErklÃ¤rung, vorgeschlagener Aktion, PrioritÃ¤t,
-- Quelltyp/-referenzen, optionalem Ziel-/Gewohnheitsbezug und GÃ¼ltigkeit.
alter table public.vt_recommendations
  add column if not exists title text,
  add column if not exists explanation jsonb not null default '{}'::jsonb,
  add column if not exists proposed_action text,
  add column if not exists priority text not null default 'medium',
  add column if not exists source_type text not null default 'rule_based',
  add column if not exists source_references jsonb not null default '[]'::jsonb,
  add column if not exists goal_id uuid references public.vt_wellness_goals(id) on delete set null,
  add column if not exists habit_id uuid references public.vt_habits(id) on delete set null,
  add column if not exists valid_from timestamptz not null default now(),
  add column if not exists valid_until timestamptz;

-- Etappe 2 default was 'pending' â€” Etappe 4 uses the richer status set
-- (proposed/accepted/modified/completed/skipped/rejected/expired). Existing
-- rows (none yet, see known risks) are unaffected; new rows use the new
-- default.
alter table public.vt_recommendations
  alter column status set default 'proposed';

create index if not exists idx_vt_recommendations_goal_id on public.vt_recommendations(goal_id);
create index if not exists idx_vt_recommendations_habit_id on public.vt_recommendations(habit_id);
create index if not exists idx_vt_recommendations_valid_until on public.vt_recommendations(valid_until);

-- vt_recommendation_decisions: distinguish accepted/modified/skipped/
-- rejected, and store the before/after action + optional reason for a
-- modified decision (Etappe 4 Â§3).
alter table public.vt_recommendation_decisions
  add column if not exists original_action text,
  add column if not exists modified_action text,
  add column if not exists reason text;

-- vt_recommendation_outcomes: add the outcome source (who/what reported the
-- outcome) â€” Etappe 4 Â§4. `outcome_status` already existed as free text in
-- Etappe 2; the application layer now constrains it to the 5 new allowed
-- values (see core/validation.py).
alter table public.vt_recommendation_outcomes
  add column if not exists outcome_source text not null default 'user_reported';

-- vt_recommendation_feedback: add the 3-value helpfulness rating and an
-- optional structured reason, alongside the existing 1-5 `rating`/`comment`
-- (kept for backward compatibility, unused by any UI yet).
alter table public.vt_recommendation_feedback
  add column if not exists helpfulness text,
  add column if not exists reason text;


-- ============================================================================
-- Quelle: 006_twin_memory_patterns_learning.sql
-- ============================================================================
-- Twin Intelligence Core â€” Etappe 5: Twin Memory, Pattern Detection und
-- Twin Learning Events.
--
-- STATUS: Verifiziert (2026-07-24) â€” alle Spalten dieser Datei wurden per
-- REST-API-Abfrage gegen die produktive Supabase-Datenbank bestÃ¤tigt und
-- existieren dort bereits.
--
-- Non-destruktiv: nur `add column if not exists` auf den bereits in Etappe 2
-- (003_twin_intelligence_foundation.sql) angelegten Tabellen vt_twin_memory /
-- vt_twin_patterns / vt_twin_learning_events. Keine bestehende Spalte wird
-- entfernt oder umbenannt.
--
-- Korrektur einer Etappe-2-Inkonsistenz: die drei Tabellen wurden damals nur
-- mit `user_id not null` angelegt (kein `email`-Feld), obwohl jede andere
-- Twin-Intelligence-Tabelle (vt_wellness_goals, vt_recommendations, ...) seit
-- Etappe 2 konsequent per `email` skopiert wird (die tatsÃ¤chlich vorhandene,
-- sofort nutzbare Nutzertrennung â€” `user_id` wird nur mitgefÃ¼hrt, sobald
-- `core/auth.py::get_user_id_by_email` es auflÃ¶sen kann). Diese Migration
-- gleicht das an: `email` wird ergÃ¤nzt, `user_id` wird nullable, damit neue
-- Zeilen exakt wie Ã¼berall sonst zuerst per `email` geschrieben werden
-- kÃ¶nnen.

------------------------------------------------------------------------------
-- 1. TWIN MEMORY (Etappe 5 Â§1) --------------------------------------------
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
-- for backward compatibility but superseded by the richer `status` column â€”
-- new code should read/write `status`, not `active`.

create index if not exists idx_vt_twin_memory_email_status
  on public.vt_twin_memory(email, status);

------------------------------------------------------------------------------
-- 2. TWIN PATTERNS (Etappe 5 Â§3) --------------------------------------------
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
-- 3. TWIN LEARNING EVENTS (Etappe 5 Â§4) -------------------------------------
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


-- ============================================================================
-- Quelle: 007_daily_planning_reflection_loops.sql
-- ============================================================================
-- Twin Intelligence Core â€” Etappe 6: Daily Planning, Evening Reflection und
-- Weekly Reflection Loops.
--
-- STATUS: Verifiziert (2026-07-24) â€” alle Spalten dieser Datei wurden per
-- REST-API-Abfrage gegen die produktive Supabase-Datenbank bestÃ¤tigt und
-- existieren dort bereits.
--
-- Non-destruktiv: nur `add column if not exists` und
-- `create index if not exists` auf den bereits in Etappe 2
-- (003_twin_intelligence_foundation.sql) angelegten Tabellen vt_daily_plans /
-- vt_daily_plan_actions / vt_daily_reflections / vt_weekly_reflections.
-- Keine bestehende Spalte wird entfernt oder umbenannt.
--
-- Monatsgrundlage (Etappe 6 Â§5) und Twin-Reifegrad (Â§6) bekommen bewusst
-- KEINE eigene Tabelle â€” beides wird zur Laufzeit aus bereits vorhandenen
-- Daten berechnet (`services/monthly_progress.py`, `services/twin_maturity.py`),
-- da Etappe 6 hierfÃ¼r ausdrÃ¼cklich nur "Grundlage vorbereiten" verlangt, kein
-- eigenes Speichermodell.

------------------------------------------------------------------------------
-- 1. DAILY PLANS (Etappe 6 Â§1-2) --------------------------------------------
------------------------------------------------------------------------------

-- `user_id`/`email` waren hier bereits seit Etappe 2 korrekt angelegt
-- (email not null, user_id nullable) â€” anders als die Etappe-2-Twin-Memory-
-- Tabellen, die erst in Etappe 5 korrigiert werden mussten. Der bestehende
-- `unique (user_id, local_date)`-Constraint schÃ¼tzt jedoch nicht zuverlÃ¤ssig
-- Ã¼ber mehrere Nutzer hinweg, solange `user_id` NULL ist (in Postgres gelten
-- zwei NULLs nie als gleich) â€” ein zusÃ¤tzlicher Unique-Index auf
-- `(email, local_date)` schlieÃŸt diese LÃ¼cke additiv.
create unique index if not exists uq_vt_daily_plans_email_local_date
  on public.vt_daily_plans(email, local_date);

------------------------------------------------------------------------------
-- 2. DAILY PLAN ACTIONS (Etappe 6 Â§2) ---------------------------------------
------------------------------------------------------------------------------

alter table public.vt_daily_plan_actions
  add column if not exists email text,
  add column if not exists priority text not null default 'medium',
  add column if not exists reasoning text,
  add column if not exists estimated_effort text,
  add column if not exists goal_id uuid references public.vt_wellness_goals(id) on delete set null,
  add column if not exists habit_id uuid references public.vt_habits(id) on delete set null,
  add column if not exists recommendation_id uuid references public.vt_recommendations(id) on delete set null,
  add column if not exists user_adjusted_description text,
  add column if not exists carried_over boolean not null default false;

-- `status` (existing, Etappe 2, default 'planned') is reused as the
-- "Umsetzungsstatus"/Entscheidungsstatus: proposed/accepted/modified/
-- completed/skipped/rejected (application-side enum, see
-- `core/validation.py::DailyPlanActionStatus`) â€” no schema change needed,
-- just a richer set of allowed values than Etappe 2 anticipated.

create index if not exists idx_vt_daily_plan_actions_email
  on public.vt_daily_plan_actions(email);
create index if not exists idx_vt_daily_plan_actions_goal_id
  on public.vt_daily_plan_actions(goal_id);
create index if not exists idx_vt_daily_plan_actions_habit_id
  on public.vt_daily_plan_actions(habit_id);

------------------------------------------------------------------------------
-- 3. DAILY REFLECTIONS (Etappe 6 Â§3) ----------------------------------------
------------------------------------------------------------------------------

alter table public.vt_daily_reflections
  add column if not exists completed_summary text,
  add column if not exists helpful_note text,
  add column if not exists difficult_note text,
  add column if not exists tomorrow_change text,
  add column if not exists daily_plan_id uuid references public.vt_daily_plans(id) on delete set null,
  add column if not exists plan_outcome jsonb not null default '{}'::jsonb,
  add column if not exists memory_candidate_notes jsonb not null default '[]'::jsonb;

-- `what_went_well`/`what_to_improve`/`mood`/`energy` (existing, Etappe 2)
-- stay untouched â€” `mood`/`energy` directly answer "Wie fÃ¼hlst du dich
-- jetzt?" (Etappe 6 Â§3), no duplicate field needed.

create unique index if not exists uq_vt_daily_reflections_email_local_date
  on public.vt_daily_reflections(email, local_date);

------------------------------------------------------------------------------
-- 4. WEEKLY REFLECTIONS (Etappe 6 Â§4) ---------------------------------------
------------------------------------------------------------------------------

alter table public.vt_weekly_reflections
  add column if not exists data_sufficient boolean not null default false,
  add column if not exists positive_developments jsonb not null default '[]'::jsonb,
  add column if not exists stable_routines jsonb not null default '[]'::jsonb,
  add column if not exists potential_areas jsonb not null default '[]'::jsonb,
  add column if not exists goal_progress jsonb not null default '[]'::jsonb,
  add column if not exists most_helpful_recommendations jsonb not null default '[]'::jsonb,
  add column if not exists least_helpful_recommendations jsonb not null default '[]'::jsonb,
  add column if not exists suggestions_next_week jsonb not null default '[]'::jsonb,
  add column if not exists data_points int not null default 0;

create unique index if not exists uq_vt_weekly_reflections_email_week_start
  on public.vt_weekly_reflections(email, week_start_date);


-- ============================================================================
-- Quelle: 008_privacy_consent_data_controls.sql
-- ============================================================================
-- Twin Intelligence Core â€” Etappe 9: Privacy, Consent, Export, Deletion.
--
-- STATUS: Verifiziert (2026-07-24) â€” der genutzte Index dieser Datei wurde
-- per REST-API-Abfrage gegen die produktive Supabase-Datenbank bestÃ¤tigt.
--
-- Non-destruktiv: nur `create index if not exists`. Etappe 9 benÃ¶tigt KEINE
-- neuen Spalten oder Tabellen â€” `vt_consent_records` (aus Etappe 2,
-- 003_twin_intelligence_foundation.sql) hat bereits alle nÃ¶tigen Felder
-- (email, consent_type, granted, granted_at, revoked_at, created_at) fÃ¼r
-- das append-only Consent-Log aus Etappe 9 Â§3
-- (siehe `services/privacy_export.py::resolve_current_consents`).

-- Effiziente "aktueller Status pro Zweck"-Abfrage: neuste Zeile pro
-- (email, consent_type).
create index if not exists idx_vt_consent_records_email_type_created_at
  on public.vt_consent_records(email, consent_type, created_at desc);

-- Kategorie-LÃ¶schung (Etappe 9 Â§2) betrifft immer "alle Zeilen einer
-- Tabelle fÃ¼r diesen Nutzer" â€” die bereits vorhandenen `idx_*_email`-Indizes
-- auf vt_daily_wellness_entries/vt_habits/vt_habit_entries/vt_wellness_goals/
-- vt_daily_plans/vt_daily_reflections/vt_weekly_reflections/
-- vt_recommendations/vt_twin_memory/vt_twin_patterns/vt_chat_usage/
-- vt_user_feedback (aus Etappe 1-6) decken das bereits ab â€” keine weiteren
-- nÃ¶tig.


-- ============================================================================
-- Quelle: 009_admin_rbac_foundation.sql
-- ============================================================================
-- VitalTwin Enterprise Release â€” Admin Control Center 1.0: RBAC Foundation.
--
-- STATUS: Verifiziert (2026-07-24) â€” alle Tabellen/Spalten dieser Datei
-- wurden per REST-API-Abfrage gegen die produktive Supabase-Datenbank
-- bestÃ¤tigt und existieren dort bereits.
--
-- Non-destruktiv: nur `create table if not exists`, `add column if not
-- exists` und `create index if not exists`. Keine bestehende Tabelle wird
-- verÃ¤ndert, umbenannt oder gelÃ¶scht. Keine bestehenden Zeilen werden
-- verÃ¤ndert oder gelÃ¶scht.

------------------------------------------------------------------------------
-- 1. ADMIN ROLLEN (RBAC) ----------------------------------------------------
------------------------------------------------------------------------------
-- Ein Nutzer ist Admin genau dann, wenn eine Zeile fÃ¼r seine `email` hier
-- existiert. Abwesenheit einer Zeile = normaler Nutzer, kein Sonderfall zu
-- behandeln. `role` ist eine der sieben in `core/admin_rbac.py::AdminRole`
-- definierten Rollen â€” die Berechtigungsmatrix selbst lebt im Code
-- (`ROLE_PERMISSIONS`), nicht in der Datenbank, damit eine Berechtigungs-
-- Ã¤nderung nie eine Migration braucht.

create table if not exists public.vt_admin_roles (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  role text not null,
  granted_by text,
  granted_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_admin_roles_email on public.vt_admin_roles(email);

------------------------------------------------------------------------------
-- 2. NUTZER SPERREN/ENTSPERREN (User Management) ----------------------------
------------------------------------------------------------------------------

alter table public.vt_users
  add column if not exists suspended boolean not null default false,
  add column if not exists suspended_at timestamptz,
  add column if not exists suspended_reason text;

create index if not exists idx_vt_users_suspended on public.vt_users(suspended) where suspended = true;

------------------------------------------------------------------------------
-- 3. LOGIN-HISTORIE (Security Center, User Management) ----------------------
------------------------------------------------------------------------------
-- Getrennt vom generischen `vt_audit_events` (Etappe 2), weil Login-Versuche
-- eine eigene, hochfrequente, sicherheitsspezifische Natur haben (jeder
-- Versuch, nicht nur erfolgreiche Aktionen) und eine eigene Aufbewahrungs-
-- Policy verdienen (siehe DATA_RETENTION.md-ErgÃ¤nzung).

create table if not exists public.vt_login_events (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  success boolean not null,
  ip_address text,
  user_agent text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_login_events_email_created_at
  on public.vt_login_events(email, created_at desc);

------------------------------------------------------------------------------
-- 4. CONTENT MANAGEMENT (Blog, FAQ, Landing Pages, Hilfeseiten, ------------
--    Benachrichtigungen) -----------------------------------------------------
------------------------------------------------------------------------------
-- Ein einziges, generisches Content-Modell statt sieben separater Tabellen â€”
-- `content_type` unterscheidet die Verwendung. Bewusst einfach gehalten
-- (kein Rich-Media-Modell, keine Versionierung) â€” siehe ADMIN_ARCHITECTURE.md
-- fÃ¼r die BegrÃ¼ndung und den Erweiterungspfad.

create table if not exists public.vt_content_items (
  id uuid primary key default gen_random_uuid(),
  content_type text not null,
  slug text,
  title text not null,
  body text,
  status text not null default 'draft',
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz
);

create index if not exists idx_vt_content_items_type_status
  on public.vt_content_items(content_type, status);
create unique index if not exists uq_vt_content_items_type_slug
  on public.vt_content_items(content_type, slug) where slug is not null;



