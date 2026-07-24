-- VitalTwin Release 0 — Platform Foundation & Integration Architecture.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`. Keine bestehende Tabelle wird veraendert,
-- umbenannt oder geloescht.
--
-- Diese Migration legt ausschliesslich Schema fuer Funktionen an, die
-- entweder bereits real genutzt werden (in-app Benachrichtigungen,
-- Feature-Flags) oder als reine Datenstruktur fuer spaetere Integrationen
-- vorbereitet sind (Affiliate/Gutscheine — noch an kein echtes Netzwerk
-- angebunden, siehe core/integrations.py).

------------------------------------------------------------------------------
-- 1. IN-APP BENACHRICHTIGUNGEN (echt genutzt, siehe routers/notifications.py)
------------------------------------------------------------------------------

create table if not exists public.vt_notifications (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  title text not null,
  body text not null,
  read boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_notifications_email_created_at
  on public.vt_notifications(email, created_at desc);
create index if not exists idx_vt_notifications_email_unread
  on public.vt_notifications(email) where read = false;

------------------------------------------------------------------------------
-- 2. FEATURE FLAGS (echt genutzt, siehe /api/admin/feature-flags)
------------------------------------------------------------------------------

create table if not exists public.vt_feature_flags (
  key text primary key,
  enabled boolean not null default false,
  description text not null default '',
  updated_by text,
  updated_at timestamptz not null default now()
);

------------------------------------------------------------------------------
-- 3. AFFILIATE / GUTSCHEINE (Schema-Vorbereitung — kein Netzwerk angebunden)
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_partners (
  id uuid primary key default gen_random_uuid(),
  network text not null,
  partner_name text not null,
  partner_code text not null,
  status text not null default 'inactive',
  created_at timestamptz not null default now(),
  unique (network, partner_code)
);

create table if not exists public.vt_affiliate_clicks (
  id uuid primary key default gen_random_uuid(),
  partner_id uuid references public.vt_affiliate_partners(id) on delete cascade,
  referrer text,
  landing_path text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_affiliate_clicks_partner_id_created_at
  on public.vt_affiliate_clicks(partner_id, created_at desc);

create table if not exists public.vt_affiliate_sales (
  id uuid primary key default gen_random_uuid(),
  partner_id uuid references public.vt_affiliate_partners(id) on delete cascade,
  email text,
  amount numeric,
  currency text not null default 'eur',
  commission_amount numeric,
  status text not null default 'pending',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_affiliate_sales_partner_id_created_at
  on public.vt_affiliate_sales(partner_id, created_at desc);

create table if not exists public.vt_coupons (
  id uuid primary key default gen_random_uuid(),
  code text not null unique,
  discount_percent numeric,
  discount_amount numeric,
  currency text not null default 'eur',
  max_redemptions int,
  redeemed_count int not null default 0,
  valid_from timestamptz not null default now(),
  valid_until timestamptz,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_coupons_code on public.vt_coupons(code);
