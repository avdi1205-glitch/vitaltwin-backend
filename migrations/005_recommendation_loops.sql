-- Twin Intelligence Core — Etappe 4: Recommendation-, Decision-, Outcome- und
-- Feedback-Loops.
--
-- STATUS: Verifiziert (2026-07-24) — alle Spalten dieser Datei wurden per
-- REST-API-Abfrage gegen die produktive Supabase-Datenbank bestätigt und
-- existieren dort bereits.
--
-- Non-destruktiv: nur `add column if not exists` auf den in Etappe 2
-- angelegten Tabellen vt_recommendations / vt_recommendation_decisions /
-- vt_recommendation_outcomes / vt_recommendation_feedback. Keine bestehende
-- Spalte wird entfernt oder umbenannt.

-- vt_recommendations: die Etappe-2-Version hatte nur category/text/source/
-- confidence/status. Etappe 4 §1 verlangt zusätzlich ein strukturiertes
-- Empfehlungsmodell mit Titel, Erklärung, vorgeschlagener Aktion, Priorität,
-- Quelltyp/-referenzen, optionalem Ziel-/Gewohnheitsbezug und Gültigkeit.
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

-- Etappe 2 default was 'pending' — Etappe 4 uses the richer status set
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
-- modified decision (Etappe 4 §3).
alter table public.vt_recommendation_decisions
  add column if not exists original_action text,
  add column if not exists modified_action text,
  add column if not exists reason text;

-- vt_recommendation_outcomes: add the outcome source (who/what reported the
-- outcome) — Etappe 4 §4. `outcome_status` already existed as free text in
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
