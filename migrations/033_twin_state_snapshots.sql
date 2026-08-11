-- Twin Core Phase 7: Persistent Longitudinal Twin State.
--
-- Reuses the EXISTING `vt_twin_context_snapshots` table (created in
-- migration 003, Etappe 2 foundation) instead of creating a second,
-- near-duplicate table — confirmed via a full grep of the codebase that
-- this table has ZERO real read/write call sites anywhere (only referenced
-- defensively inside `core/account_deletion.py`'s purge loop, which has
-- been silently unable to actually delete anything from it since the
-- table never had an `email` column to filter on until this migration).
-- Its existing shape (id / user_id / snapshot jsonb / reason text /
-- created_at) already matches almost exactly what a Twin State Snapshot
-- needs — extending it is smaller and safer than a parallel table.
--
-- Same identity-consistency fix already applied to vt_twin_memory /
-- vt_twin_patterns / vt_twin_learning_events in migration 006: this table
-- was only ever created with `user_id not null`, while every other Twin
-- Core table is scoped primarily by `email` (the immediately-usable
-- separation — `user_id` is populated only when
-- `core/auth.py::get_user_id_by_email` can resolve it). Aligning this
-- table the same way; non-destructive, no data ever existed in it to lose.

alter table public.vt_twin_context_snapshots
  alter column user_id drop not null;

alter table public.vt_twin_context_snapshots
  add column if not exists email text,
  add column if not exists snapshot_version int not null default 1,
  add column if not exists change_summary jsonb not null default '{}'::jsonb;

create index if not exists idx_vt_twin_context_snapshots_email_created_at
  on public.vt_twin_context_snapshots(email, created_at desc);

notify pgrst, 'reload schema';
