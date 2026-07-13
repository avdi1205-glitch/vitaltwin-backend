-- Run this in Supabase SQL Editor to persist VitalTwin users and premium state.
create table if not exists public.vt_users (
  id bigint generated always as identity primary key,
  email text not null unique,
  full_name text not null,
  password text not null,
  premium boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create or replace function public.set_updated_at_vt_users()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_set_updated_at_vt_users on public.vt_users;
create trigger trg_set_updated_at_vt_users
before update on public.vt_users
for each row execute function public.set_updated_at_vt_users();
