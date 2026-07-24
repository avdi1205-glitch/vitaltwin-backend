-- VitalTwin Enterprise Release — Admin Control Center 1.0: RBAC Foundation.
--
-- STATUS: Entwurf, NICHT gegen eine echte Datenbank ausgeführt (siehe alle
-- vorherigen Migrationsheader — kein DB-Zugriff in dieser Session verfügbar).
--
-- Non-destruktiv: nur `create table if not exists`, `add column if not
-- exists` und `create index if not exists`. Keine bestehende Tabelle wird
-- verändert, umbenannt oder gelöscht. Keine bestehenden Zeilen werden
-- verändert oder gelöscht.

------------------------------------------------------------------------------
-- 1. ADMIN ROLLEN (RBAC) ----------------------------------------------------
------------------------------------------------------------------------------
-- Ein Nutzer ist Admin genau dann, wenn eine Zeile für seine `email` hier
-- existiert. Abwesenheit einer Zeile = normaler Nutzer, kein Sonderfall zu
-- behandeln. `role` ist eine der sieben in `core/admin_rbac.py::AdminRole`
-- definierten Rollen — die Berechtigungsmatrix selbst lebt im Code
-- (`ROLE_PERMISSIONS`), nicht in der Datenbank, damit eine Berechtigungs-
-- änderung nie eine Migration braucht.

create table if not exists public.vt_admin_roles (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  role text not null,
  granted_by text,
  granted_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_admin_roles_email on public.vt_admin_roles(email);

------------------------------------------------------------------------------
-- 2. NUTZER SPERREN/ENTSPERREN (User Management) ----------------------------
------------------------------------------------------------------------------

alter table public.vt_users
  add column if not exists suspended boolean not null default false,
  add column if not exists suspended_at timestamptz,
  add column if not exists suspended_reason text;

create index if not exists idx_vt_users_suspended on public.vt_users(suspended) where suspended = true;

------------------------------------------------------------------------------
-- 3. LOGIN-HISTORIE (Security Center, User Management) ----------------------
------------------------------------------------------------------------------
-- Getrennt vom generischen `vt_audit_events` (Etappe 2), weil Login-Versuche
-- eine eigene, hochfrequente, sicherheitsspezifische Natur haben (jeder
-- Versuch, nicht nur erfolgreiche Aktionen) und eine eigene Aufbewahrungs-
-- Policy verdienen (siehe DATA_RETENTION.md-Ergänzung).

create table if not exists public.vt_login_events (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  success boolean not null,
  ip_address text,
  user_agent text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_login_events_email_created_at
  on public.vt_login_events(email, created_at desc);

------------------------------------------------------------------------------
-- 4. CONTENT MANAGEMENT (Blog, FAQ, Landing Pages, Hilfeseiten, ------------
--    Benachrichtigungen) -----------------------------------------------------
------------------------------------------------------------------------------
-- Ein einziges, generisches Content-Modell statt sieben separater Tabellen —
-- `content_type` unterscheidet die Verwendung. Bewusst einfach gehalten
-- (kein Rich-Media-Modell, keine Versionierung) — siehe ADMIN_ARCHITECTURE.md
-- für die Begründung und den Erweiterungspfad.

create table if not exists public.vt_content_items (
  id uuid primary key default gen_random_uuid(),
  content_type text not null,
  slug text,
  title text not null,
  body text,
  status text not null default 'draft',
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz
);

create index if not exists idx_vt_content_items_type_status
  on public.vt_content_items(content_type, status);
create unique index if not exists uq_vt_content_items_type_slug
  on public.vt_content_items(content_type, slug) where slug is not null;
