-- VitalTwin Enterprise — Founder Operating System, Submodul D: Smart
-- Approval Center.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`.
--
-- WICHTIG (Modul-Abgrenzung): Diese Tabelle gehoert ausschliesslich zum
-- Founder Operating System. Sie verarbeitet keine Gesundheits-, CGM-,
-- Nutrition-, Schlaf- oder Twin-Memory-Daten -- nur Metadaten ueber
-- Business-/Technik-/Content-Vorschlaege (Affiliate-Produkte, Partner,
-- Support-Feedback, ...).
--
-- `dedupe_key` verhindert Duplikate/Spam: pro real erkannter Bedingung
-- (z. B. ein bestimmtes Produkt wartet auf Freigabe) existiert maximal
-- eine Zeile -- genau wie in vt_founder_tasks (Migration 013).

create table if not exists public.vt_founder_approvals (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  title text not null,
  category text not null,
  source text not null,
  priority text not null default 'mittel',
  status text not null default 'ki_geprueft',
  reason text not null,
  data_used text not null,
  rules_applied text not null,
  benefits text not null,
  risks text not null,
  founder_comment text,
  related_entity_type text,
  related_entity_id text,
  auto_detected boolean not null default true,
  decided_at timestamptz,
  decided_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_approvals_status on public.vt_founder_approvals(status);
create index if not exists idx_vt_founder_approvals_category on public.vt_founder_approvals(category);
create index if not exists idx_vt_founder_approvals_priority on public.vt_founder_approvals(priority);
create index if not exists idx_vt_founder_approvals_created_at on public.vt_founder_approvals(created_at desc);
