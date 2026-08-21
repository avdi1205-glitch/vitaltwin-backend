-- Migration 042: Accounting foundation for GoBD-oriented bookkeeping
-- handover to a future Steuerberater — adds Google AdSense earnings as a
-- second tracked revenue source alongside the existing Stripe tables
-- (vt_stripe_subscriptions/vt_stripe_payments/vt_stripe_refunds, migration
-- 023). The read/write logic built on top of this lives in
-- `backend/app/core/adsense_billing.py` (import) and
-- `backend/app/core/accounting_export.py` (CSV/DATEV export, read-only) —
-- this migration only adds the two new tables those modules use.
--
-- STATUS: written, NOT yet executed against production Supabase — run
-- manually in the Supabase SQL Editor, same convention as every other
-- migration in this repo.
--
-- WHY TWO TABLES (GoBD "Unveränderbarkeit" / append-only): AdSense's own
-- CSV export has no stable per-row ID the way a Stripe webhook event does
-- (`event.id`) — and Google can restate/correct a day's earnings after
-- the fact (e.g. invalid-traffic reversals). Instead of an upsert-by-date
-- (which would silently overwrite a previously-recorded figure — exactly
-- what GoBD forbids), every CSV import creates exactly ONE new
-- `vt_adsense_import_batches` row (who/when/which file — the
-- "Nachvollziehbarkeit" anchor standing in for AdSense's missing
-- per-row ID) and N `vt_adsense_earnings` rows that reference it.
-- Corrections are meant to be new rows referencing the row they correct
-- via `reverses_earning_id`, never an UPDATE/DELETE of the original (the
-- column exists so `core/adsense_billing.py` or a future admin action can
-- record a correction this way; nothing in this migration performs
-- corrections automatically). `raw_row_hash` gives a second,
-- content-based idempotency check so re-uploading the exact same export
-- twice does not double-count revenue (see the unique index below).
--
-- Non-destructive: only `create table if not exists`/`create index if not
-- exists`, no existing table touched, no data anywhere deleted or
-- rewritten.
--
-- SECURITY (RLS): same convention as `vt_stripe_*` (migration 023) — RLS
-- disabled, access controlled entirely at the FastAPI layer via
-- `require_admin_permission(..., "view_accounting" | "manage_accounting")`
-- (see `core/admin_rbac.py` — both permissions are super_admin-only by
-- default, same precedent as the CEO Intelligence/Autopilot modules,
-- given this data is destined for direct Steuerberater/tax handover).

-- ---------------------------------------------------------------------------
-- 1. Import batches — one row per CSV upload, the traceability anchor.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_adsense_import_batches (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  imported_by text not null,
  source_filename text,
  row_count integer not null default 0,
  skipped_duplicate_count integer not null default 0,
  notes text
);

create index if not exists idx_vt_adsense_import_batches_created_at
  on public.vt_adsense_import_batches (created_at desc);

alter table public.vt_adsense_import_batches disable row level security;

-- ---------------------------------------------------------------------------
-- 2. Earnings — append-only ledger, one row per (report_date, country) line
--    from an AdSense CSV export. Gross revenue stored in cents (bigint),
--    matching this repo's existing `vt_stripe_payments.amount_paid`
--    convention (migration 023) rather than a float.
-- ---------------------------------------------------------------------------
create table if not exists public.vt_adsense_earnings (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  report_date date not null,
  country text,
  gross_revenue_cents bigint not null,
  currency text not null default 'eur',
  import_batch_id bigint not null references public.vt_adsense_import_batches(id),
  raw_row_hash text not null,
  entry_type text not null default 'original',
  reverses_earning_id bigint references public.vt_adsense_earnings(id)
);

alter table public.vt_adsense_earnings
  drop constraint if exists vt_adsense_earnings_entry_type_check;
alter table public.vt_adsense_earnings
  add constraint vt_adsense_earnings_entry_type_check check (entry_type in ('original', 'correction'));

create index if not exists idx_vt_adsense_earnings_report_date
  on public.vt_adsense_earnings (report_date desc);
create index if not exists idx_vt_adsense_earnings_batch
  on public.vt_adsense_earnings (import_batch_id);

-- Idempotency guard: the exact same CSV row content can never be inserted
-- twice (content-based, via raw_row_hash) — a correction with a genuinely
-- different amount produces a different hash and is let through.
create unique index if not exists uq_vt_adsense_earnings_dedupe
  on public.vt_adsense_earnings (report_date, coalesce(country, ''), raw_row_hash);

alter table public.vt_adsense_earnings disable row level security;

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';
