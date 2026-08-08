-- Extends the existing single content model (`vt_content_items`, from
-- migration 009) with optional editorial metadata fields needed for a real
-- content-editing workflow (excerpt, category, tags, meta title/description).
-- Deliberately NOT a new table — same model used by blog/faq/landing_page/
-- help_page/notification content types.
--
-- Non-destruktiv: nur `add column if not exists`. Keine bestehende Spalte
-- wird veraendert, umbenannt oder geloescht.

alter table public.vt_content_items
  add column if not exists excerpt text,
  add column if not exists category text,
  add column if not exists tags text[] not null default '{}',
  add column if not exists meta_title text,
  add column if not exists meta_description text;
