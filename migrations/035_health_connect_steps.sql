-- Health Connect (Android on-device) steps ingestion — Phase 2.
--
-- health_activity_records.connection_id was a hard NOT NULL FK to
-- user_health_connections (an OAuth-shaped table: access/refresh tokens,
-- granted_scopes, ...). Health Connect has no OAuth "connection" concept at
-- all (it's a direct on-device permission grant, verified via Android's own
-- Health Connect docs) — reusing this SAME table for its steps records
-- requires connection_id to be nullable for non-OAuth providers. No existing
-- Google Health row is touched; connection_id stays NOT NULL-equivalent in
-- practice for every row Google Health itself ever writes.
alter table public.health_activity_records
  alter column connection_id drop not null;

-- Formalizes the one new allowed provider value (previously unconstrained,
-- default 'google_health' only) so a typo can never silently mislabel data.
alter table public.health_activity_records
  add constraint health_activity_records_provider_check
  check (provider in ('google_health', 'health_connect'));
