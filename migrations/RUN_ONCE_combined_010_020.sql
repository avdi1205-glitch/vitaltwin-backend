-- ============================================================================
-- KOMBINIERTES SKRIPT: Migrationen 010-020 in einem Stueck (Convenience-Datei)
-- Nur zum einmaligen Einfuegen in den Supabase SQL Editor gedacht.
-- Die einzelnen, nummerierten Dateien in migrations/ bleiben die Quelle der
-- Wahrheit -- diese Datei ist nur eine Zusammenfassung fuer den Rollout des
-- Founder Operating System (Kontaktformular + Platform Foundation +
-- Affiliate Platform + Founder OS Submodule A-J).
--
-- Alle Statements sind additiv und idempotent (create table/index if not
-- exists, alter table add column if not exists) -- ein erneuter Lauf ist
-- sicher, falls Teile bereits ausgefuehrt wurden.
-- ============================================================================

-- ============================================================================
-- Quelle: 010_contact_messages.sql
-- ============================================================================
-- Kontaktformular (/kontakt) -- TMG-Anforderung an einen zweiten schnellen
-- Kontaktweg neben der E-Mail-Adresse im Impressum (BGH-Urteil I ZR 93/08:
-- E-Mail allein reicht in der Regel nicht, es braucht einen weiteren Weg zur
-- unmittelbaren Kommunikation).
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`. Keine bestehende Tabelle wird veraendert,
-- umbenannt oder geloescht.

create table if not exists public.vt_contact_messages (
  id uuid primary key default gen_random_uuid(),
  full_name text not null,
  email text not null,
  subject text,
  message text not null,
  source text not null default 'kontakt-page',
  status text not null default 'new',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_contact_messages_created_at
  on public.vt_contact_messages(created_at desc);
create index if not exists idx_vt_contact_messages_status
  on public.vt_contact_messages(status) where status = 'new';


-- ============================================================================
-- Quelle: 011_platform_foundation.sql
-- ============================================================================
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


-- ============================================================================
-- Quelle: 012_affiliate_platform.sql
-- ============================================================================
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


-- ============================================================================
-- Quelle: 013_founder_task_manager.sql
-- ============================================================================
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


-- ============================================================================
-- Quelle: 014_founder_approval_center.sql
-- ============================================================================
-- VitalTwin Enterprise — Founder Operating System, Submodul D: Smart
-- Approval Center.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`.
--
-- WICHTIG (Modul-Abgrenzung): Diese Tabelle gehoert ausschliesslich zum
-- Founder Operating System. Sie verarbeitet keine Gesundheits-, CGM-,
-- Nutrition-, Schlaf- oder Twin-Memory-Daten -- nur Metadaten ueber
-- Business-/Technik-/Content-Vorschlaege (Affiliate-Produkte, Partner,
-- Support-Feedback, ...).
--
-- `dedupe_key` verhindert Duplikate/Spam: pro real erkannter Bedingung
-- (z. B. ein bestimmtes Produkt wartet auf Freigabe) existiert maximal
-- eine Zeile -- genau wie in vt_founder_tasks (Migration 013).

create table if not exists public.vt_founder_approvals (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  title text not null,
  category text not null,
  source text not null,
  priority text not null default 'mittel',
  status text not null default 'ki_geprueft',
  reason text not null,
  data_used text not null,
  rules_applied text not null,
  benefits text not null,
  risks text not null,
  founder_comment text,
  related_entity_type text,
  related_entity_id text,
  auto_detected boolean not null default true,
  decided_at timestamptz,
  decided_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_approvals_status on public.vt_founder_approvals(status);
create index if not exists idx_vt_founder_approvals_category on public.vt_founder_approvals(category);
create index if not exists idx_vt_founder_approvals_priority on public.vt_founder_approvals(priority);
create index if not exists idx_vt_founder_approvals_created_at on public.vt_founder_approvals(created_at desc);


-- ============================================================================
-- Quelle: 015_founder_business_coach.sql
-- ============================================================================
-- VitalTwin Enterprise — Founder Operating System, Submodul E: AI
-- Business Coach.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` und
-- `create index if not exists`.
--
-- WICHTIG (Modul-Abgrenzung): Diese Tabellen gehoeren ausschliesslich zum
-- Founder Operating System. Sie speichern NIEMALS individuelle Wellness-,
-- CGM-, Nutrition-, Schlaf-, Bewegungs- oder Twin-Memory-Daten -- nur
-- aggregierte Geschaeftskennzahlen und Meta-Text (Titel, Begruendungen).
--
-- Bewusst NICHT angelegt (siehe docs/AI_BUSINESS_COACH.md §Datenmodell):
-- FounderBusinessRisk / FounderBusinessOpportunity als eigene Tabellen
-- (Chancen/Risiken sind Insights, gefiltert nach `category`) und
-- FounderMetricSnapshot (Kennzahlen werden bei jedem Aufruf frisch aus
-- den bereits zeitgestempelten Rohdaten berechnet, keine Zwischenspeicherung
-- noetig) -- um keine doppelte, ueberlappende Infrastruktur zu bauen.

------------------------------------------------------------------------------
-- 1. FounderBusinessGoal
------------------------------------------------------------------------------

create table if not exists public.vt_founder_business_goals (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  category text not null,
  start_value numeric,
  target_value numeric not null,
  start_date date,
  target_date date,
  status text not null default 'geplant',
  current_progress numeric,
  data_source text not null default 'nicht verbunden',
  responsible_module text,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_business_goals_status on public.vt_founder_business_goals(status);
create index if not exists idx_vt_founder_business_goals_category on public.vt_founder_business_goals(category);

------------------------------------------------------------------------------
-- 2. FounderBusinessInsight (deckt auch "Chancen" und "Risiken" ab --
--    per category-Filter, keine separaten Tabellen)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_business_insights (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  title text not null,
  category text not null,
  description text not null,
  data_basis text not null,
  period_start date,
  period_end date,
  comparison_period_start date,
  comparison_period_end date,
  severity text not null default 'mittel',
  confidence text not null default 'mittel',
  possible_cause text,
  possible_impact text,
  recommended_action text,
  estimated_effort text,
  expected_benefit text,
  status text not null default 'erkannt',
  source text not null default 'regelbasiert',
  source_references jsonb not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_business_insights_category on public.vt_founder_business_insights(category);
create index if not exists idx_vt_founder_business_insights_status on public.vt_founder_business_insights(status);
create index if not exists idx_vt_founder_business_insights_severity on public.vt_founder_business_insights(severity);

------------------------------------------------------------------------------
-- 3. FounderBusinessRecommendation
------------------------------------------------------------------------------

create table if not exists public.vt_founder_business_recommendations (
  id uuid primary key default gen_random_uuid(),
  insight_id uuid references public.vt_founder_business_insights(id) on delete set null,
  title text not null,
  reasoning text not null,
  data_basis text not null,
  expected_benefit text,
  risk text,
  effort text,
  priority text not null default 'mittel',
  success_metric text,
  test_period text,
  status text not null default 'offen',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_business_recommendations_status on public.vt_founder_business_recommendations(status);

------------------------------------------------------------------------------
-- 4. FounderCoachQuery + FounderCoachResponse (eine Tabelle: Frage+Antwort
--    gehoeren untrennbar zusammen)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_coach_queries (
  id uuid primary key default gen_random_uuid(),
  question text not null,
  answer text,
  insufficient_data boolean not null default false,
  ai_provider text,
  latency_ms int,
  error text,
  created_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_coach_queries_created_at on public.vt_founder_coach_queries(created_at desc);

------------------------------------------------------------------------------
-- 5. FounderAutomationEvent (fuer den Automation Score)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_automation_events (
  id uuid primary key default gen_random_uuid(),
  event_type text not null,
  reference_table text,
  reference_id text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_automation_events_type on public.vt_founder_automation_events(event_type);
create index if not exists idx_vt_founder_automation_events_created_at on public.vt_founder_automation_events(created_at desc);


-- ============================================================================
-- Quelle: 016_founder_affiliate_intelligence.sql
-- ============================================================================
-- VitalTwin Enterprise — Founder Operating System, Submodul F: Affiliate
-- Intelligence.
--
-- STATUS: Entwurf, noch nicht gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `alter table ... add column if not exists`,
-- `create table if not exists` und `create index if not exists`.
--
-- WICHTIG (keine doppelte Arbeit): Dieses Submodul baut NICHT auf einer
-- zweiten, parallelen Produktdatenbank. Es ERWEITERT die bestehende
-- vt_affiliate_products-Tabelle (aus Migration 012) um die zusaetzlichen
-- Felder, die fuer Normalisierung/Datenqualitaet/Verfuegbarkeit noetig
-- sind, und legt nur EINE wirklich neue Tabelle an
-- (vt_affiliate_duplicate_candidates) fuer ein Konzept, das vorher nicht
-- existierte (Beziehung zwischen zwei moeglicherweise doppelten Produkten).
--
-- Provider-Zugangsdaten (`vt_affiliate_partners.api_key` etc.), Tracking
-- (`vt_affiliate_events`), Blacklist (`vt_affiliate_blacklist`), Kampagnen
-- (`vt_affiliate_campaigns`), A/B-Tests (`vt_affiliate_ab_tests`) und
-- Nutzerpraeferenzen (`vt_affiliate_user_prefs`) existieren bereits seit
-- Migration 012 und werden hier NICHT dupliziert.

------------------------------------------------------------------------------
-- 1. Erweiterung von vt_affiliate_products (Normalisierung/Datenqualitaet)
------------------------------------------------------------------------------

alter table public.vt_affiliate_products add column if not exists data_quality_score numeric;
alter table public.vt_affiliate_products add column if not exists availability text not null default 'unknown';
alter table public.vt_affiliate_products add column if not exists normalization_version int not null default 1;
alter table public.vt_affiliate_products add column if not exists ai_reviewed boolean not null default false;
alter table public.vt_affiliate_products add column if not exists sensitive_category boolean not null default false;
alter table public.vt_affiliate_products add column if not exists external_product_id text;
alter table public.vt_affiliate_products add column if not exists provider_raw_ref text;
alter table public.vt_affiliate_products add column if not exists review_reasons jsonb not null default '[]';

------------------------------------------------------------------------------
-- 2. Dublettenerkennung — einzige wirklich neue Tabelle
------------------------------------------------------------------------------

create table if not exists public.vt_affiliate_duplicate_candidates (
  id uuid primary key default gen_random_uuid(),
  product_a_id uuid not null references public.vt_affiliate_products(id) on delete cascade,
  product_b_id uuid not null references public.vt_affiliate_products(id) on delete cascade,
  match_reason text not null,
  status text not null default 'moegliches_duplikat',
  resolved_by text,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (product_a_id, product_b_id)
);

create index if not exists idx_vt_affiliate_duplicate_candidates_status on public.vt_affiliate_duplicate_candidates(status);


-- ============================================================================
-- Quelle: 017_founder_automation_engine.sql
-- ============================================================================
-- VitalTwin Enterprise — Founder Operating System, Submodul G: Automation
-- Engine.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Non-destruktiv: nur `create table if not exists` / `create index if not
-- exists` / additive `alter table ... add column if not exists`.
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.
-- Nur Founder-OS-Automationsregeln, deren Ausfuehrungshistorie und
-- abgeleitete, aggregierte Prozessdaten.
--
-- Kein Hintergrund-Scheduler/Queue existiert in dieser Codebase (Railway,
-- Single-Prozess, keine Celery/Redis-Queue). "Zeitgesteuerte" Regeln
-- werden -- konsistent mit jedem anderen Founder-OS-Submodul -- beim
-- Laden des Dashboards ODER durch einen expliziten Aufruf von
-- `POST /api/admin/founder/automation/run-due` ausgewertet (z. B. durch
-- einen externen Cron-Aufruf). Es gibt keine serverseitige Dauerschleife.

------------------------------------------------------------------------------
-- 1. AutomationRule (+ Versionierung)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_rules (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  description text not null default '',
  category text not null,
  trigger_type text not null,
  trigger_config jsonb not null default '{}'::jsonb,
  conditions jsonb not null default '[]'::jsonb,
  actions jsonb not null default '[]'::jsonb,
  risk_level text not null,
  approval_policy text not null default 'no_approval',
  retry_policy jsonb not null default '{"type": "none", "max_attempts": 1, "cooldown_seconds": 60}'::jsonb,
  timeout_seconds int not null default 30,
  max_runs int,
  run_count int not null default 0,
  enabled boolean not null default false,
  status text not null default 'entwurf',
  environment text not null default 'production',
  rollout_stage text not null default 'nur_founder',
  approved_once boolean not null default false,
  version int not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  created_by text,
  last_run_at timestamptz,
  next_run_at timestamptz
);

create index if not exists idx_vt_automation_rules_status on public.vt_automation_rules(status);
create index if not exists idx_vt_automation_rules_category on public.vt_automation_rules(category);
create index if not exists idx_vt_automation_rules_enabled on public.vt_automation_rules(enabled);
create index if not exists idx_vt_automation_rules_next_run_at on public.vt_automation_rules(next_run_at);

create table if not exists public.vt_automation_rule_versions (
  id uuid primary key default gen_random_uuid(),
  rule_id uuid not null references public.vt_automation_rules(id) on delete cascade,
  version int not null,
  snapshot jsonb not null,
  created_at timestamptz not null default now(),
  created_by text,
  unique (rule_id, version)
);

create index if not exists idx_vt_automation_rule_versions_rule_id on public.vt_automation_rule_versions(rule_id);

------------------------------------------------------------------------------
-- 2. AutomationRun (inkl. Steps, Retry, Rollback)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_runs (
  id uuid primary key default gen_random_uuid(),
  rule_id uuid references public.vt_automation_rules(id) on delete set null,
  idempotency_key text not null unique,
  trigger_type text not null,
  trigger_signature text,
  status text not null default 'wartend',
  risk_level text,
  environment text,
  attempt int not null default 1,
  max_attempts int not null default 1,
  dry_run boolean not null default false,
  steps jsonb not null default '[]'::jsonb,
  result jsonb,
  error text,
  previous_state jsonb,
  rollback_status text,
  rollback_at timestamptz,
  rollback_by text,
  approval_id text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_runs_rule_id on public.vt_automation_runs(rule_id);
create index if not exists idx_vt_automation_runs_status on public.vt_automation_runs(status);
create index if not exists idx_vt_automation_runs_created_at on public.vt_automation_runs(created_at desc);

------------------------------------------------------------------------------
-- 3. AutomationDeadLetter
------------------------------------------------------------------------------

create table if not exists public.vt_automation_dead_letters (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references public.vt_automation_runs(id) on delete set null,
  rule_id uuid references public.vt_automation_rules(id) on delete set null,
  reason text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_dead_letters_rule_id on public.vt_automation_dead_letters(rule_id);

------------------------------------------------------------------------------
-- 4. AutomationOpportunity (Vorschlaege, nie automatisch aktiviert)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_opportunities (
  id uuid primary key default gen_random_uuid(),
  signature text not null unique,
  category text,
  description text not null,
  occurrences int not null default 1,
  source_table text not null,
  suggested_rule jsonb,
  status text not null default 'neu',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_opportunities_status on public.vt_automation_opportunities(status);

------------------------------------------------------------------------------
-- 5. AutomationAlert (dedupliziert, priorisiert)
------------------------------------------------------------------------------

create table if not exists public.vt_automation_alerts (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  severity text not null default 'mittel',
  title text not null,
  message text not null,
  category text,
  source_run_id uuid references public.vt_automation_runs(id) on delete set null,
  status text not null default 'offen',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_automation_alerts_status on public.vt_automation_alerts(status);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';


-- ============================================================================
-- Quelle: 018_founder_ceo_intelligence.sql
-- ============================================================================
-- VitalTwin Enterprise — Founder Operating System, Submodul H: CEO
-- Intelligence.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Bewusst NUR 2 neue Tabellen: CEO Intelligence ist ueberwiegend eine
-- Aggregations-/Syntheseschicht ueber die bereits bestehenden Founder-OS-
-- Tabellen (vt_founder_business_goals, vt_founder_business_insights,
-- vt_founder_tasks, vt_founder_approvals, vt_automation_*) -- siehe
-- docs/CEO_INTELLIGENCE.md fuer die vollstaendige Begruendung, warum
-- KEIN ExecutiveGoal/ExecutiveInsight/ExecutiveRisk/ExecutiveOpportunity/
-- ExecutiveScorecard/ExecutiveSummary als eigene Tabelle angelegt wird.
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.

------------------------------------------------------------------------------
-- 1. ExecutiveScenario (gespeicherte Was-waere-wenn-Simulationen)
------------------------------------------------------------------------------

create table if not exists public.vt_executive_scenarios (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  scenario_type text not null,
  assumptions jsonb not null default '{}'::jsonb,
  results jsonb not null default '{}'::jsonb,
  computable boolean not null default true,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_executive_scenarios_created_at on public.vt_executive_scenarios(created_at desc);

------------------------------------------------------------------------------
-- 2. ExecutiveQuery ("Frag CEO Intelligence")
------------------------------------------------------------------------------

create table if not exists public.vt_executive_queries (
  id uuid primary key default gen_random_uuid(),
  question text not null,
  answer text,
  insufficient_data boolean not null default false,
  ai_provider text,
  latency_ms int,
  error text,
  created_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_executive_queries_created_at on public.vt_executive_queries(created_at desc);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';


-- ============================================================================
-- Quelle: 019_founder_auto_documentation.sql
-- ============================================================================
-- VitalTwin Enterprise — Founder Operating System, Submodul I: Auto
-- Documentation.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen
-- (wie alle vorherigen Migrationen in diesem Ordner).
--
-- Wichtige Architektur-Entscheidung (siehe docs/AUTO_DOCUMENTATION.md):
-- Dieses Submodul schreibt NIEMALS auf das Dateisystem (weder Backend-
-- noch Frontend-Repository). "Generierte Dokumentation" wird
-- ausschliesslich als Text in `generated_content`/`vt_documentation_
-- versions` gespeichert -- der Gruender kopiert daraus manuell in echte
-- .md-Dateien, falls gewuenscht. Das eliminiert jedes Path-Traversal-/
-- Ueberschreibungs-Risiko vollstaendig (kein Schreibpfad existiert).
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.

------------------------------------------------------------------------------
-- 1. DocumentationRegistry (+ Versionierung)
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_registry (
  id uuid primary key default gen_random_uuid(),
  document_path text not null unique,
  title text not null,
  category text not null,
  module text not null default 'founder_os',
  submodule text,
  owner text,
  status text not null default 'draft',
  source_files jsonb not null default '[]'::jsonb,
  last_generated_at timestamptz,
  last_reviewed_at timestamptz,
  last_approved_at timestamptz,
  version int not null default 1,
  content_hash text,
  source_hash text,
  is_generated boolean not null default false,
  requires_approval boolean not null default false,
  protected boolean not null default false,
  stale_reason text,
  generated_content text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_documentation_registry_status on public.vt_documentation_registry(status);
create index if not exists idx_vt_documentation_registry_category on public.vt_documentation_registry(category);
create index if not exists idx_vt_documentation_registry_module on public.vt_documentation_registry(module);

create table if not exists public.vt_documentation_versions (
  id uuid primary key default gen_random_uuid(),
  registry_id uuid not null references public.vt_documentation_registry(id) on delete cascade,
  version int not null,
  content text,
  content_hash text,
  diff_summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  created_by text,
  unique (registry_id, version)
);

create index if not exists idx_vt_documentation_versions_registry_id on public.vt_documentation_versions(registry_id);

------------------------------------------------------------------------------
-- 2. DocumentationGenerationRun
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_generation_runs (
  id uuid primary key default gen_random_uuid(),
  run_type text not null,
  status text not null default 'laeuft',
  items_scanned int not null default 0,
  items_updated int not null default 0,
  items_flagged_stale int not null default 0,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_documentation_generation_runs_created_at on public.vt_documentation_generation_runs(created_at desc);

------------------------------------------------------------------------------
-- 3. DocumentationChangeProposal (fuer geschuetzte Dokumente)
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_change_proposals (
  id uuid primary key default gen_random_uuid(),
  registry_id uuid references public.vt_documentation_registry(id) on delete set null,
  proposed_content text,
  diff_summary jsonb not null default '{}'::jsonb,
  reason text,
  risk_level text not null default 'mittel',
  status text not null default 'offen',
  approval_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_documentation_change_proposals_status on public.vt_documentation_change_proposals(status);

------------------------------------------------------------------------------
-- 4. DocumentationQuery ("Frag die Projektdokumentation")
------------------------------------------------------------------------------

create table if not exists public.vt_documentation_queries (
  id uuid primary key default gen_random_uuid(),
  question text not null,
  answer text,
  insufficient_data boolean not null default false,
  ai_provider text,
  latency_ms int,
  error text,
  created_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_documentation_queries_created_at on public.vt_documentation_queries(created_at desc);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';


-- ============================================================================
-- Quelle: 020_founder_autopilot.sql
-- ============================================================================
-- VitalTwin Enterprise — Founder Operating System, Submodul J: Founder
-- Autopilot.
--
-- STATUS: Entwurf, noch NICHT gegen die produktive Supabase-Datenbank
-- ausgefuehrt -- bitte einmalig manuell im Supabase SQL-Editor einfuegen.
--
-- Founder Autopilot ist ueberwiegend eine Orchestrierungs-/Aggregations-
-- schicht ueber die bereits bestehenden Submodule A-I (siehe
-- docs/FOUNDER_AUTOPILOT.md) -- nur 5 neue, kleine Tabellen fuer Zustand,
-- Policies, Events, Alerts und Incidents/Kill-Switch. "Daily Plan" und
-- "Weekly Review" werden wie ueberall sonst frisch bei Abruf berechnet,
-- nicht gespeichert.
--
-- Kein Feld dieser Migration speichert individuelle Wellness-, CGM-,
-- Ernaehrungs-, Schlaf-, Bewegungs-, Biomarker- oder Twin-Memory-Daten.

------------------------------------------------------------------------------
-- 1. Autopilot-Zustand (Modus-Historie; aktueller Zustand = neueste Zeile)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_state (
  id uuid primary key default gen_random_uuid(),
  mode text not null default 'assist',
  kill_switch_active boolean not null default false,
  incident_mode_active boolean not null default false,
  reason text,
  created_at timestamptz not null default now(),
  created_by text
);

create index if not exists idx_vt_founder_autopilot_state_created_at on public.vt_founder_autopilot_state(created_at desc);

------------------------------------------------------------------------------
-- 2. Autopilot Policies
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_policies (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  description text not null default '',
  mode text not null default 'assist',
  allowed_categories jsonb not null default '[]'::jsonb,
  blocked_categories jsonb not null default '[]'::jsonb,
  maximum_risk_level text not null default 'low',
  approval_policy text not null default 'always_require_approval',
  financial_threshold numeric,
  execution_window jsonb not null default '{}'::jsonb,
  allowed_environments jsonb not null default '["production"]'::jsonb,
  rollback_required boolean not null default true,
  audit_required boolean not null default true,
  enabled boolean not null default false,
  status text not null default 'entwurf',
  version int not null default 1,
  previous_versions jsonb not null default '[]'::jsonb,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_policies_status on public.vt_founder_autopilot_policies(status);

------------------------------------------------------------------------------
-- 3. Autopilot Events (synthetisiert aus A-I, on-read dedupliziert)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_events (
  id uuid primary key default gen_random_uuid(),
  event_type text not null,
  source_module text not null,
  source_id text,
  severity text not null default 'information',
  occurred_at timestamptz not null default now(),
  payload_reference text,
  dedupe_key text not null unique,
  handled_at timestamptz,
  status text not null default 'offen',
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_events_status on public.vt_founder_autopilot_events(status);
create index if not exists idx_vt_founder_autopilot_events_occurred_at on public.vt_founder_autopilot_events(occurred_at desc);

------------------------------------------------------------------------------
-- 4. Autopilot Alerts (dedupliziert, priorisiert, eskalierbar)
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_alerts (
  id uuid primary key default gen_random_uuid(),
  dedupe_key text not null unique,
  severity text not null default 'mittel',
  title text not null,
  message text not null,
  category text,
  status text not null default 'offen',
  escalated boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_alerts_status on public.vt_founder_autopilot_alerts(status);

------------------------------------------------------------------------------
-- 5. Incidents + Kill-Switch-Ereignisse
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_incidents (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  reason text not null,
  status text not null default 'aktiv',
  activated_by text,
  activated_at timestamptz not null default now(),
  resolved_at timestamptz,
  resolved_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.vt_founder_autopilot_kill_switch_events (
  id uuid primary key default gen_random_uuid(),
  action text not null,
  reason text,
  performed_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_kill_switch_events_created_at on public.vt_founder_autopilot_kill_switch_events(created_at desc);

------------------------------------------------------------------------------
-- 6. Autopilot Queries ("Frag Founder Autopilot")
------------------------------------------------------------------------------

create table if not exists public.vt_founder_autopilot_queries (
  id uuid primary key default gen_random_uuid(),
  question text not null,
  answer text,
  insufficient_data boolean not null default false,
  error text,
  created_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_vt_founder_autopilot_queries_created_at on public.vt_founder_autopilot_queries(created_at desc);

-- Reload PostgREST schema cache.
notify pgrst, 'reload schema';


