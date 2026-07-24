-- VitalTwin Enterprise Release — Affiliate Intelligence & Management Platform.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists`, `create index if not
-- exists` und `alter table ... add column if not exists`. Keine
-- bestehende Tabelle wird umbenannt oder geloescht.
--
-- Erweitert die bereits in 011_platform_foundation.sql angelegten
-- Affiliate-Tabellen (vt_affiliate_partners/clicks/sales, vt_coupons) um
-- das vollstaendige Produkt-/Kategorie-/Kampagnen-/Tracking-/Blacklist-
-- /Praeferenzen-Schema fuer das Affiliate Center (siehe
-- docs/AFFILIATE_PLATFORM.md).

------------------------------------------------------------------------------
-- 1. PARTNERPROGRAMME (erweitert vt_affiliate_partners aus Migration 011)
------------------------------------------------------------------------------

alter table public.vt_affiliate_partners add column if not exists api_available boolean not null default false;
alter table public.vt_affiliate_partners add column if not exists api_key text;
alter table public.vt_affiliate_partners add column if not exists tracking_id text;
alter table public.vt_affiliate_partners add column if not exists commission_rate numeric;
alter table public.vt_affiliate_partners add column if not exists cookie_duration_days int;
alter table public.vt_affiliate_partners add column if not exists notes text not null default '';
alter table public.vt_affiliate_partners add column if not exists updated_at timestamptz not null default now();

------------------------------------------------------------------------------
-- 2. KATEGORIEN
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_categories (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  slug text not null unique,
  created_at timestamptz not null default now()
);

------------------------------------------------------------------------------
-- 3. PRODUKTE
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_products (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  subtitle text,
  category_id uuid references public.vt_affiliate_categories(id) on delete set null,
  brand text,
  manufacturer text,
  description text,
  image_url text,
  price numeric,
  currency text not null default 'eur',
  affiliate_url text not null,
  deep_link text,
  partner_id uuid references public.vt_affiliate_partners(id) on delete set null,
  commission_rate numeric,
  tags text[] not null default '{}',
  target_audience text,
  region text not null default 'DE',
  language text not null default 'de',
  status text not null default 'draft',
  priority int not null default 0,
  rating numeric,
  notes text not null default '',
  start_date date,
  end_date date,
  pinned boolean not null default false,
  link_status text not null default 'unchecked',
  link_http_status int,
  link_last_checked_at timestamptz,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_affiliate_products_status on public.vt_affiliate_products(status);
create index if not exists idx_vt_affiliate_products_category_id on public.vt_affiliate_products(category_id);
create index if not exists idx_vt_affiliate_products_partner_id on public.vt_affiliate_products(partner_id);

------------------------------------------------------------------------------
-- 4. BLACKLIST (Produkte, Marken, Partner, Kategorien)
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_blacklist (
  id uuid primary key default gen_random_uuid(),
  entry_type text not null check (entry_type in ('product', 'brand', 'partner', 'category')),
  value text not null,
  reason text,
  created_by text,
  created_at timestamptz not null default now(),
  unique (entry_type, value)
);

------------------------------------------------------------------------------
-- 5. SAISONALE KAMPAGNEN
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_campaigns (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  season text,
  start_date date,
  end_date date,
  product_ids uuid[] not null default '{}',
  active boolean not null default true,
  created_by text,
  created_at timestamptz not null default now()
);

------------------------------------------------------------------------------
-- 6. A/B TESTS
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_ab_tests (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  product_a_id uuid references public.vt_affiliate_products(id) on delete cascade,
  product_b_id uuid references public.vt_affiliate_products(id) on delete cascade,
  status text not null default 'active',
  winner text,
  created_by text,
  created_at timestamptz not null default now()
);

------------------------------------------------------------------------------
-- 7. TRACKING (Impressionen / Klicks / Conversions, je Produkt)
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_events (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references public.vt_affiliate_products(id) on delete cascade,
  ab_test_id uuid references public.vt_affiliate_ab_tests(id) on delete set null,
  ab_test_variant text,
  event_type text not null check (event_type in ('impression', 'click', 'conversion')),
  email text,
  revenue numeric,
  commission numeric,
  context jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_affiliate_events_product_id_created_at
  on public.vt_affiliate_events(product_id, created_at desc);
create index if not exists idx_vt_affiliate_events_type_created_at
  on public.vt_affiliate_events(event_type, created_at desc);

------------------------------------------------------------------------------
-- 8. KI TRANSPARENZ — warum wurde ein Produkt empfohlen?
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_recommendation_log (
  id uuid primary key default gen_random_uuid(),
  email text,
  product_id uuid references public.vt_affiliate_products(id) on delete cascade,
  category text,
  rule_applied text not null,
  reason text,
  context jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_affiliate_recommendation_log_email_created_at
  on public.vt_affiliate_recommendation_log(email, created_at desc);

------------------------------------------------------------------------------
-- 9. NUTZERKONTROLLE (Opt-out, ausgeblendete Kategorien/Produkte)
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_user_prefs (
  email text primary key,
  affiliate_enabled boolean not null default true,
  hidden_categories text[] not null default '{}',
  hidden_products uuid[] not null default '{}',
  updated_at timestamptz not null default now()
);
