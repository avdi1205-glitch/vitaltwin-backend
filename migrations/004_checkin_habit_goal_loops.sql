-- Twin Intelligence Core — Etappe 3: Check-in-, Habit- und Goal-Loops.
--
-- STATUS: Verifiziert (2026-07-24) — alle Spalten dieser Datei wurden per
-- REST-API-Abfrage gegen die produktive Supabase-Datenbank bestätigt und
-- existieren dort bereits.
--
-- Non-destruktiv: nur `add column if not exists`. Ergänzt die in Etappe 2
-- bereits erweiterten Tabellen um die für Etappe 3 zusätzlich benötigten
-- Felder, statt neue parallele Tabellen anzulegen.

-- Check-in (vt_daily_wellness_entries): Bewegung in Minuten (zusätzlich zu
-- movement_days_per_week) und eine kurze optionale Notiz. `energy`/`stress`
-- sind die neuen 1-10-Skalen aus Etappe 3 §1 — bewusst zusätzlich zu den
-- bestehenden `energy_level`/`stress_level` (1-5, weiterhin vom
-- Marker-Formular auf dem Dashboard genutzt), nicht als Ersatz, um dessen
-- bestehende Auswahl (1-5-Dropdown) nicht zu brechen.
alter table public.vt_daily_wellness_entries
  add column if not exists movement_minutes int,
  add column if not exists note text,
  add column if not exists energy int,
  add column if not exists stress int;

-- Habits: dritter Zustand "pausiert" zusätzlich zu aktiv/archiviert. `active`
-- (boolean) bleibt für Abwärtskompatibilität bestehen und wird von der
-- Anwendungsschicht synchron zu `status` gehalten (status='active' <=>
-- active=true), damit keine bestehende Abfrage bricht.
alter table public.vt_habits
  add column if not exists status text not null default 'active';

create index if not exists idx_vt_habits_email_status on public.vt_habits(email, status);

-- Wellness goals (vt_wellness_goals, created in Etappe 2): a human-readable
-- title wasn't part of the original Etappe 2 schema (which only stored
-- goal_type/status/target_*) — Etappe 3 §6 explicitly requires one.
alter table public.vt_wellness_goals
  add column if not exists title text;

