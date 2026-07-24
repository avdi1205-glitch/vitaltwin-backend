-- Twin Intelligence Core — Etappe 2: Datenmodelle, Migrationen, Validierung
-- und Nutzertrennung.
--
-- STATUS: Verifiziert (2026-07-24) — alle Tabellen/Spalten dieser Datei
-- wurden per REST-API-Abfrage gegen die produktive Supabase-Datenbank
-- bestätigt und existieren dort bereits. Diese Datei ist weiterhin sicher
-- erneut ausführbar (siehe Non-destruktiv-Hinweis unten), z. B. beim Aufsetzen
-- einer neuen Umgebung.
--
-- Non-destruktiv: ausschließlich `create table if not exists`,
-- `add column if not exists` und `create index if not exists`. Keine
-- bestehende Tabelle wird verändert, umbenannt oder gelöscht. Keine
-- bestehenden Zeilen werden verändert oder gelöscht.
--
-- Reihenfolge wichtig: vt_users muss vor allen `references vt_users(id)`
-- existieren (siehe SUPABASE_SCHEMA.sql) — das ist bereits der Fall.

------------------------------------------------------------------------------
-- 1. BESTEHENDE MODELLE ERWEITERN (statt Duplikate zu erzeugen)
------------------------------------------------------------------------------
-- Jede Tabelle bekommt zusätzlich zur bisherigen email-Spalte eine nullable
-- user_id-Spalte (FK auf vt_users.id). email bleibt die Quelle der Wahrheit,
-- bis ein separates Backfill-Skript (spätere Etappe, mit ausdrücklicher
-- Freigabe) user_id für Bestandsdaten befüllt. Neuer Code sollte user_id
-- bevorzugt setzen, wenn verfügbar.

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
-- vt_user_profiles.wellness_goals (text[]) bleibt für die schnelle
-- Onboarding-Mehrfachauswahl bestehen. vt_wellness_goals ist eine neue,
-- eigenständige Entität für den vollen Goal Loop (Ziel -> Plan -> Umsetzung
-- -> Bewertung -> neuer Plan) mit Status und Zieldatum — kein Duplikat,
-- sondern eine andere Granularität (Auswahl-Tag vs. verfolgbares Ziel).

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
-- Formular auf dem Dashboard unverändert. vt_recommendation_feedback ist
-- gezielt an eine einzelne Recommendation gebunden — andere Granularität,
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
