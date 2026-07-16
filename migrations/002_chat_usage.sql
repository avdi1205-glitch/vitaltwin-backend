-- Block 6: "Frag deinen Twin" wellness assistant — server-side daily usage
-- counter (rate limiting must not be frontend-only). Non-destructive: only
-- creates a new table/index, does not alter or drop any existing table.
--
-- Deliberately does NOT store chat message content by default (see Block 6
-- report): only a per-user, per-day request counter, since the product does
-- not yet offer a "view your chat history" feature. If that changes later,
-- add a separate vt_chat_messages table with a clear retention/deletion policy.

create table if not exists public.vt_chat_usage (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  usage_date date not null,
  count int not null default 0,
  last_request_at timestamptz,
  created_at timestamptz not null default now(),
  unique (email, usage_date)
);

create index if not exists idx_vt_chat_usage_email on public.vt_chat_usage(email);
