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
