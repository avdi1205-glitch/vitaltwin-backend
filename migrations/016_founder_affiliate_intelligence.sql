-- VitalTwin Enterprise — Founder Operating System, Submodul F: Affiliate
-- Intelligence.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `alter table ... add column if not exists`,
-- `create table if not exists` und `create index if not exists`.
--
-- WICHTIG (keine doppelte Arbeit): Dieses Submodul baut NICHT auf einer
-- zweiten, parallelen Produktdatenbank. Es ERWEITERT die bestehende
-- vt_affiliate_products-Tabelle (aus Migration 012) um die zusaetzlichen
-- Felder, die fuer Normalisierung/Datenqualitaet/Verfuegbarkeit noetig
-- sind, und legt nur EINE wirklich neue Tabelle an
-- (vt_affiliate_duplicate_candidates) fuer ein Konzept, das vorher nicht
-- existierte (Beziehung zwischen zwei moeglicherweise doppelten Produkten).
--
-- Provider-Zugangsdaten (`vt_affiliate_partners.api_key` etc.), Tracking
-- (`vt_affiliate_events`), Blacklist (`vt_affiliate_blacklist`), Kampagnen
-- (`vt_affiliate_campaigns`), A/B-Tests (`vt_affiliate_ab_tests`) und
-- Nutzerpraeferenzen (`vt_affiliate_user_prefs`) existieren bereits seit
-- Migration 012 und werden hier NICHT dupliziert.

------------------------------------------------------------------------------
-- 1. Erweiterung von vt_affiliate_products (Normalisierung/Datenqualitaet)
------------------------------------------------------------------------------

alter table public.vt_affiliate_products add column if not exists data_quality_score numeric;
alter table public.vt_affiliate_products add column if not exists availability text not null default 'unknown';
alter table public.vt_affiliate_products add column if not exists normalization_version int not null default 1;
alter table public.vt_affiliate_products add column if not exists ai_reviewed boolean not null default false;
alter table public.vt_affiliate_products add column if not exists sensitive_category boolean not null default false;
alter table public.vt_affiliate_products add column if not exists external_product_id text;
alter table public.vt_affiliate_products add column if not exists provider_raw_ref text;
alter table public.vt_affiliate_products add column if not exists review_reasons jsonb not null default '[]';

------------------------------------------------------------------------------
-- 2. Dublettenerkennung — einzige wirklich neue Tabelle
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_duplicate_candidates (
  id uuid primary key default gen_random_uuid(),
  product_a_id uuid not null references public.vt_affiliate_products(id) on delete cascade,
  product_b_id uuid not null references public.vt_affiliate_products(id) on delete cascade,
  match_reason text not null,
  status text not null default 'moegliches_duplikat',
  resolved_by text,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (product_a_id, product_b_id)
);

create index if not exists idx_vt_affiliate_duplicate_candidates_status on public.vt_affiliate_duplicate_candidates(status);
