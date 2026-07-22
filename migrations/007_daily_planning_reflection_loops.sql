-- Twin Intelligence Core — Etappe 6: Daily Planning, Evening Reflection und
-- Weekly Reflection Loops.
--
-- STATUS: Entwurf, NICHT gegen eine echte Datenbank ausgeführt (siehe
-- Etappe-2..5-Berichte: kein DB-Zugriff in dieser Session verfügbar).
--
-- Non-destruktiv: nur `add column if not exists` und
-- `create index if not exists` auf den bereits in Etappe 2
-- (003_twin_intelligence_foundation.sql) angelegten Tabellen vt_daily_plans /
-- vt_daily_plan_actions / vt_daily_reflections / vt_weekly_reflections.
-- Keine bestehende Spalte wird entfernt oder umbenannt.
--
-- Monatsgrundlage (Etappe 6 §5) und Twin-Reifegrad (§6) bekommen bewusst
-- KEINE eigene Tabelle — beides wird zur Laufzeit aus bereits vorhandenen
-- Daten berechnet (`services/monthly_progress.py`, `services/twin_maturity.py`),
-- da Etappe 6 hierfür ausdrücklich nur "Grundlage vorbereiten" verlangt, kein
-- eigenes Speichermodell.

------------------------------------------------------------------------------
-- 1. DAILY PLANS (Etappe 6 §1-2) --------------------------------------------
------------------------------------------------------------------------------

-- `user_id`/`email` waren hier bereits seit Etappe 2 korrekt angelegt
-- (email not null, user_id nullable) — anders als die Etappe-2-Twin-Memory-
-- Tabellen, die erst in Etappe 5 korrigiert werden mussten. Der bestehende
-- `unique (user_id, local_date)`-Constraint schützt jedoch nicht zuverlässig
-- über mehrere Nutzer hinweg, solange `user_id` NULL ist (in Postgres gelten
-- zwei NULLs nie als gleich) — ein zusätzlicher Unique-Index auf
-- `(email, local_date)` schließt diese Lücke additiv.
create unique index if not exists uq_vt_daily_plans_email_local_date
  on public.vt_daily_plans(email, local_date);

------------------------------------------------------------------------------
-- 2. DAILY PLAN ACTIONS (Etappe 6 §2) ---------------------------------------
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
-- `core/validation.py::DailyPlanActionStatus`) — no schema change needed,
-- just a richer set of allowed values than Etappe 2 anticipated.

create index if not exists idx_vt_daily_plan_actions_email
  on public.vt_daily_plan_actions(email);
create index if not exists idx_vt_daily_plan_actions_goal_id
  on public.vt_daily_plan_actions(goal_id);
create index if not exists idx_vt_daily_plan_actions_habit_id
  on public.vt_daily_plan_actions(habit_id);

------------------------------------------------------------------------------
-- 3. DAILY REFLECTIONS (Etappe 6 §3) ----------------------------------------
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
-- stay untouched — `mood`/`energy` directly answer "Wie fühlst du dich
-- jetzt?" (Etappe 6 §3), no duplicate field needed.

create unique index if not exists uq_vt_daily_reflections_email_local_date
  on public.vt_daily_reflections(email, local_date);

------------------------------------------------------------------------------
-- 4. WEEKLY REFLECTIONS (Etappe 6 §4) ---------------------------------------
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
