-- Stripe billing events: real subscription lifecycle, payments (revenue),
-- and refunds — populated by Stripe webhook events (backend/app/routers/
-- payments.py::stripe_webhook), never fabricated. All three tables start
-- EMPTY. Run once in Supabase SQL Editor, then `notify pgrst, 'reload
-- schema'` at the end refreshes PostgREST's cache.

-- ---------------------------------------------------------------------------
-- 1. Subscriptions — current status per Stripe subscription (mirrors
--    Stripe's own status strings: trialing/active/past_due/canceled/...).
--    Upserted on customer.subscription.created/updated/deleted.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_stripe_subscriptions (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  email text not null,
  stripe_customer_id text,
  stripe_subscription_id text not null unique,
  status text not null,
  plan_price_id text,
  current_period_end timestamptz,
  cancel_at_period_end boolean not null default false,
  canceled_at timestamptz
);

create index if not exists idx_vt_stripe_subscriptions_email
  on public.vt_stripe_subscriptions (email);
create index if not exists idx_vt_stripe_subscriptions_status
  on public.vt_stripe_subscriptions (status);

alter table public.vt_stripe_subscriptions disable row level security;

-- ---------------------------------------------------------------------------
-- 2. Payments — one row per paid Stripe invoice, the real revenue source
--    (invoice.paid webhook). amount_paid is in the smallest currency unit
--    (e.g. cents), matching Stripe's own convention.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_stripe_payments (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  email text,
  stripe_customer_id text,
  stripe_invoice_id text not null unique,
  amount_paid bigint not null,
  currency text not null default 'eur',
  paid_at timestamptz not null default now()
);

create index if not exists idx_vt_stripe_payments_paid_at
  on public.vt_stripe_payments (paid_at desc);
create index if not exists idx_vt_stripe_payments_email
  on public.vt_stripe_payments (email);

alter table public.vt_stripe_payments disable row level security;

-- ---------------------------------------------------------------------------
-- 3. Refunds — one row per Stripe refund (charge.refunded webhook).
-- ---------------------------------------------------------------------------
create table if not exists public.vt_stripe_refunds (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  email text,
  stripe_customer_id text,
  stripe_charge_id text,
  stripe_refund_id text not null unique,
  amount bigint not null,
  currency text not null default 'eur',
  reason text
);

create index if not exists idx_vt_stripe_refunds_created_at
  on public.vt_stripe_refunds (created_at desc);

alter table public.vt_stripe_refunds disable row level security;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
