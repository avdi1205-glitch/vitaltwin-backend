-- Small follow-up fix (2026-08-08): after Google Health's real Beta status
-- was confirmed (successful live OAuth connect + sync test), the wording
-- in the "was-ist-ein-digitaler-wellness-zwilling" article was outdated
-- ("Aktuell in Entwicklung"). This ONLY replaces that one sentence within
-- the existing body via `replace()` — no other text touched, no new
-- article created. Safe to re-run (no-op on a second run since the old
-- substring will no longer be present).
--
-- Run once in the Supabase SQL Editor.

update public.vt_content_items
set
  body = replace(
    body,
    '**Aktuell in Entwicklung**: eine tiefere Anbindung an Wearables über Google Health.',
    '**Beta, in Testphase**: automatische Gesundheitsdaten über Google Health — Verbinden und Synchronisieren funktionieren bereits erfolgreich, aktuell für eine begrenzte Zahl von Testnutzern.'
  ),
  updated_at = now()
where content_type = 'blog' and slug = 'was-ist-ein-digitaler-wellness-zwilling';
