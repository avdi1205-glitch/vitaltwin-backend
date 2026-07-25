"""Smart Approval Center — detection engine (VitalTwin Enterprise, Founder
Operating System, Submodule D).

**Module boundary (per spec):** this file belongs exclusively to the
Founder Operating System. It never reads or writes health, CGM, nutrition,
sleep, movement, or Twin Memory data — only Business/Technik/Content
metadata (affiliate products/partners, support feedback).

**No LLM call.** Exactly like `core/founder_task_detector.py` and
`core/affiliate_engine.py`, "KI-Prüfung" here means a deterministic rule
that inspects real rows and writes a fully-reasoned, auditable proposal —
never a free-text generation. Every proposal's `reason`/`data_used`/
`rules_applied`/`benefits`/`risks` fields are template strings built from
the actual numbers found.

**Only rules backed by a real, already-existing data source are
implemented.** The spec names 13 areas the AI assistant "darf automatisch
prüfen" (Affiliate Produkte, Neue Partnerprogramme, Defekte Links, Neue
Blogartikel, SEO Vorschläge, Neue Releases, Dokumentationsänderungen,
Support Priorisierung, API Änderungen, Neue Integrationen, Feature Flags,
Systemwarnungen, Neue Roadmap Einträge). Only four have a real signal in
this codebase and therefore an actual detection rule:

- **Affiliate Produkte** — Produkte mit `status='in_review'` (echte
  Freigabe-Entscheidung, mit direktem Durchgriff auf
  `vt_affiliate_products.status` bei Freigabe/Ablehnung).
- **Defekte Links** — Produkte mit `link_status='broken'`.
- **Abgelaufene Angebote** — Produkte mit `end_date < heute`, deren Status
  noch `approved`/`active` ist (bisher in keinem anderen Modul erkannt).
- **Neue Partnerprogramme** — Partner mit `status='inactive'` (echter
  Durchgriff auf `vt_affiliate_partners.status` bei Freigabe).
- **Support-Priorisierung** — neues Feedback seit gestern (informativ,
  kein Durchgriff, da `vt_user_feedback` kein "gelöst"-Feld hat).

Blog/SEO/Releases/Dokumentation/API/Integrationen/Feature-Flags/System-
warnungen/Roadmap haben **keine** Erkennungsregel — es gibt keinen
CMS-Redaktionsplan, keinen SEO-Crawler, keinen Release-Tracker, keine
Doku-Aktualitätsprüfung, kein API-Änderungsmonitoring, kein Roadmap-Modell
und kein System-Warnsystem in diesem Codebase. Eine erfundene Regel dafür
würde gegen "keine Fake-Daten" verstoßen — siehe
`frontend/docs/SMART_APPROVAL_CENTER.md`.

**Idempotent, ein Vorschlag pro erkannter Instanz.** Anders als
`founder_task_detector.py` (eine aggregierte Aufgabe pro Regel) erzeugt
dieses Modul **einen Vorschlag pro betroffenem Datensatz** (z. B. ein
Vorschlag pro wartendem Produkt), weil hier tatsächlich einzeln
freigegeben/abgelehnt werden soll — nicht nur "zur Kenntnis genommen".
`dedupe_key` enthält daher die Entity-ID. Wurde ein Vorschlag bereits vom
Gründer entschieden (`freigegeben`/`abgelehnt`/`archiviert`), wird er nie
erneut erzeugt oder überschrieben.
"""

from __future__ import annotations

from datetime import date, timedelta, timezone, datetime

from .supabase import supabase

APPROVAL_TABLE = "vt_founder_approvals"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
AFFILIATE_PARTNER_TABLE = "vt_affiliate_partners"
FEEDBACK_TABLE = "vt_user_feedback"

TERMINAL_STATUSES = ("freigegeben", "abgelehnt", "archiviert")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _existing(dedupe_key: str) -> dict | None:
    try:
        rows = supabase.table(APPROVAL_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    except Exception:
        return None
    return rows[0] if rows else None


def _upsert(dedupe_key: str, payload: dict) -> None:
    existing = _existing(dedupe_key)
    if existing is not None:
        if existing.get("status") in TERMINAL_STATUSES:
            return  # Founder already decided — never reopen or overwrite.
        payload["updated_at"] = _now_iso()
        try:
            supabase.table(APPROVAL_TABLE).update(payload).eq("dedupe_key", dedupe_key).execute()
        except Exception:
            pass
        return

    payload.update({"dedupe_key": dedupe_key, "status": "ki_geprueft", "auto_detected": True})
    try:
        supabase.table(APPROVAL_TABLE).insert(payload).execute()
    except Exception:
        pass


def _detect_affiliate_products_pending_approval() -> None:
    try:
        products = (
            supabase.table(AFFILIATE_PRODUCT_TABLE)
            .select("id,title,brand,created_at")
            .eq("status", "in_review")
            .execute()
            .data
            or []
        )
    except Exception:
        products = []

    for product in products:
        _upsert(
            f"affiliate_product_pending_{product['id']}",
            {
                "title": f"Affiliate-Produkt zur Freigabe: {product.get('title', '—')}",
                "category": "affiliate",
                "source": "affiliate_produkte",
                "priority": "mittel",
                "reason": "Produkt wurde von einem Admin angelegt und wartet im Status 'In Prüfung' auf eine Freigabe-Entscheidung.",
                "data_used": f"vt_affiliate_products.id={product['id']}, status='in_review', angelegt am {product.get('created_at', '—')}.",
                "rules_applied": "Regel: status == 'in_review' → Vorschlag 'Affiliate Produkte' erzeugen.",
                "benefits": "Bei Freigabe darf der Twin dieses Produkt künftig regelbasiert empfehlen (core/affiliate_engine.py).",
                "risks": "Ungeprüfte Produkte könnten einen fragwürdigen Anbieter oder eine unpassende Zielgruppe haben.",
                "related_entity_type": "affiliate_product",
                "related_entity_id": str(product["id"]),
            },
        )


def _detect_affiliate_broken_links() -> None:
    try:
        products = (
            supabase.table(AFFILIATE_PRODUCT_TABLE)
            .select("id,title,affiliate_url")
            .eq("link_status", "broken")
            .execute()
            .data
            or []
        )
    except Exception:
        products = []

    for product in products:
        _upsert(
            f"affiliate_link_broken_{product['id']}",
            {
                "title": f"Defekter Affiliate-Link: {product.get('title', '—')}",
                "category": "affiliate",
                "source": "defekte_links",
                "priority": "hoch",
                "reason": "Der letzte automatische Link-Check (core/affiliate_link_checker.py) hat diesen Link als nicht erreichbar markiert.",
                "data_used": f"vt_affiliate_products.id={product['id']}, link_status='broken', affiliate_url={product.get('affiliate_url', '—')}.",
                "rules_applied": "Regel: link_status == 'broken' → Vorschlag 'Defekte Links' erzeugen.",
                "benefits": "Frühes Erkennen verhindert, dass Nutzer auf einen toten Link klicken (Vertrauensverlust, keine Provision).",
                "risks": "Ohne Reaktion bleibt das Produkt potenziell weiter empfohlen, falls der Status nicht auch den Empfehlungsfilter blockiert (tut er — siehe core/affiliate_engine.py — aber der Link bleibt kaputt, bis er behoben wird).",
                "related_entity_type": "affiliate_product_link",
                "related_entity_id": str(product["id"]),
            },
        )


def _detect_expired_offers() -> None:
    today = date.today().isoformat()
    try:
        products = (
            supabase.table(AFFILIATE_PRODUCT_TABLE)
            .select("id,title,status,end_date")
            .execute()
            .data
            or []
        )
    except Exception:
        products = []

    for product in products:
        end_date = product.get("end_date")
        if not end_date or str(end_date) >= today or product.get("status") not in ("approved", "active"):
            continue
        _upsert(
            f"affiliate_offer_expired_{product['id']}",
            {
                "title": f"Angebot abgelaufen: {product.get('title', '—')}",
                "category": "affiliate",
                "source": "abgelaufene_angebote",
                "priority": "mittel",
                "reason": f"end_date ({end_date}) liegt in der Vergangenheit, Status ist aber noch '{product.get('status')}'.",
                "data_used": f"vt_affiliate_products.id={product['id']}, end_date={end_date}, status={product.get('status')}.",
                "rules_applied": "Regel: end_date < heute UND status in (approved, active) → Vorschlag 'Abgelaufene Angebote'.",
                "benefits": "Der Empfehlungsfilter respektiert end_date bereits (core/affiliate_engine.py) — dieser Vorschlag sorgt zusätzlich dafür, dass der Status im Admin-Bereich auch sichtbar korrigiert wird.",
                "risks": "Ohne Statuskorrektur wirkt die Produktliste unübersichtlich (abgelaufene Angebote erscheinen weiter als 'aktiv').",
                "related_entity_type": "affiliate_product",
                "related_entity_id": str(product["id"]),
            },
        )


def _detect_new_partner_programs() -> None:
    try:
        partners = (
            supabase.table(AFFILIATE_PARTNER_TABLE)
            .select("id,network,partner_name")
            .eq("status", "inactive")
            .execute()
            .data
            or []
        )
    except Exception:
        partners = []

    for partner in partners:
        _upsert(
            f"affiliate_partner_new_{partner['id']}",
            {
                "title": f"Neues Partnerprogramm: {partner.get('partner_name', '—')} ({partner.get('network', '—')})",
                "category": "business",
                "source": "neue_partnerprogramme",
                "priority": "mittel",
                "reason": "Partnerprogramm wurde angelegt, ist aber noch nicht aktiv (status='inactive').",
                "data_used": f"vt_affiliate_partners.id={partner['id']}, network={partner.get('network')}, status='inactive'.",
                "rules_applied": "Regel: status == 'inactive' → Vorschlag 'Neue Partnerprogramme'.",
                "benefits": "Bei Freigabe können diesem Partner Produkte zugeordnet und Provisionen getrackt werden.",
                "risks": "Ein aktiviertes, aber ungeprüftes Partnerprogramm könnte ungünstige Konditionen (Cookie-Laufzeit, Provision) haben.",
                "related_entity_type": "affiliate_partner",
                "related_entity_id": str(partner["id"]),
            },
        )


def _detect_support_feedback() -> None:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        rows = (
            supabase.table(FEEDBACK_TABLE).select("id,message,score,created_at").gte("created_at", yesterday).execute().data
            or []
        )
    except Exception:
        rows = []

    for row in rows:
        _upsert(
            f"support_feedback_{row['id']}",
            {
                "title": f"Support-Rückmeldung prüfen (Score {row.get('score', '—')})",
                "category": "support",
                "source": "support_priorisierung",
                "priority": "hoch" if isinstance(row.get("score"), int) and row["score"] <= 2 else "mittel",
                "reason": "Neues Nutzerfeedback seit gestern — vt_user_feedback hat kein 'gelöst'-Feld, daher zeitbasierte Erkennung statt Ticket-Status.",
                "data_used": f"vt_user_feedback.id={row['id']}, score={row.get('score')}, created_at={row.get('created_at')}.",
                "rules_applied": "Regel: created_at >= gestern → Vorschlag 'Support-Priorisierung'; Priorität 'hoch' bei score <= 2.",
                "benefits": "Frühzeitige Priorisierung verhindert, dass unzufriedene Nutzer unbeantwortet bleiben.",
                "risks": "Kein direkter Durchgriff — dieser Vorschlag ist rein informativ, keine automatische Antwort wird je gesendet.",
                "related_entity_type": None,
                "related_entity_id": str(row["id"]),
            },
        )


def run_detection() -> None:
    """Runs every implemented detection rule once. Called synchronously at
    the start of `GET /api/admin/founder/approvals` — no scheduler, no
    queue, no background job."""
    _detect_affiliate_products_pending_approval()
    _detect_affiliate_broken_links()
    _detect_expired_offers()
    _detect_new_partner_programs()
    _detect_support_feedback()
