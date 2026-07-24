-- VitalTwin Release F3 — AI Founder Task Manager.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`.
--
-- Speichert automatisch erkannte Gruender-Aufgaben (core/founder_task_
-- detector.py). `dedupe_key` verhindert Duplikate/Spam: pro Erkennungs-
-- regel gibt es maximal eine offene Aufgabe; wird das zugrunde liegende
-- Problem behoben, loest der naechste Scan die Aufgabe automatisch auf
-- (auto_resolved = true), statt sie manuell schliessen zu muessen.

create table if not exists public.vt_founder_tasks (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  title text not null,
  category text not null,
  source text not null,
  priority text not null default 'mittel',
  status text not null default 'neu',
  reason text not null,
  data_used text not null,
  impact_if_ignored text not null,
  suggested_action text,
  suggested_action_available boolean not null default false,
  auto_detected boolean not null default true,
  auto_resolved boolean not null default false,
  ignored boolean not null default false,
  remind_at timestamptz,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_tasks_status on public.vt_founder_tasks(status);
create index if not exists idx_vt_founder_tasks_priority on public.vt_founder_tasks(priority);
create index if not exists idx_vt_founder_tasks_category on public.vt_founder_tasks(category);
create index if not exists idx_vt_founder_tasks_created_at on public.vt_founder_tasks(created_at desc);
