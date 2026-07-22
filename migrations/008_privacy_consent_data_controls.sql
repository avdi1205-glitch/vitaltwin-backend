-- Twin Intelligence Core — Etappe 9: Privacy, Consent, Export, Deletion.
--
-- STATUS: Entwurf, NICHT gegen eine echte Datenbank ausgeführt (siehe
-- Etappe-2..8-Berichte: kein DB-Zugriff in dieser Session verfügbar).
--
-- Non-destruktiv: nur `create index if not exists`. Etappe 9 benötigt KEINE
-- neuen Spalten oder Tabellen — `vt_consent_records` (aus Etappe 2,
-- 003_twin_intelligence_foundation.sql) hat bereits alle nötigen Felder
-- (email, consent_type, granted, granted_at, revoked_at, created_at) für
-- das append-only Consent-Log aus Etappe 9 §3
-- (siehe `services/privacy_export.py::resolve_current_consents`).

-- Effiziente "aktueller Status pro Zweck"-Abfrage: neuste Zeile pro
-- (email, consent_type).
create index if not exists idx_vt_consent_records_email_type_created_at
  on public.vt_consent_records(email, consent_type, created_at desc);

-- Kategorie-Löschung (Etappe 9 §2) betrifft immer "alle Zeilen einer
-- Tabelle für diesen Nutzer" — die bereits vorhandenen `idx_*_email`-Indizes
-- auf vt_daily_wellness_entries/vt_habits/vt_habit_entries/vt_wellness_goals/
-- vt_daily_plans/vt_daily_reflections/vt_weekly_reflections/
-- vt_recommendations/vt_twin_memory/vt_twin_patterns/vt_chat_usage/
-- vt_user_feedback (aus Etappe 1-6) decken das bereits ab — keine weiteren
-- nötig.
