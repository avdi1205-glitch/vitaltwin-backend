"""Affiliate Intelligence — additional Task Manager detectors (VitalTwin
Enterprise, Founder Operating System, Submodule F).

**No duplicate detectors.** `core/founder_task_detector.py` (Submodule C)
already creates aggregate tasks for broken links, new products, and
products pending approval — those are NOT reimplemented here. This module
only adds detectors for the genuinely new conditions the spec asks for
that don't exist yet: missing product data, possible duplicates, and an
affiliate-revenue drop (reusing
`core/founder_business_metrics.py::get_affiliate_revenue_by_category`
rather than a new revenue computation).

Conditions explicitly requested but not implemented (no real signal
exists): fehlgeschlagene Synchronisation, Provider-API ausgefallen,
Zugangsdaten laufen ab — none of the 6 network APIs have a real
connection to fail in the first place (see `core/affiliate_provider.py`).
"""

from __future__ import annotations

from datetime import date, timedelta

from . import founder_business_metrics as metrics
from .affiliate_review_rules import missing_required_fields
from .supabase import supabase

TASK_TABLE = "vt_founder_tasks"
PRODUCT_TABLE = "vt_affiliate_products"
DUPLICATE_TABLE = "vt_affiliate_duplicate_candidates"

REVENUE_DROP_THRESHOLD_PCT = 30


def _upsert_task(dedupe_key: str, condition: bool, payload_fn) -> None:
    try:
        existing = supabase.table(TASK_TABLE).select("id,status").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    except Exception:
        return

    if not condition:
        if existing and existing[0].get("status") not in ("erledigt", "archiviert"):
            try:
                supabase.table(TASK_TABLE).update({"status": "erledigt", "auto_resolved": True}).eq("dedupe_key", dedupe_key).execute()
            except Exception:
                pass
        return

    if existing:
        if existing[0].get("status") in ("erledigt", "archiviert"):
            return
        try:
            supabase.table(TASK_TABLE).update(payload_fn()).eq("dedupe_key", dedupe_key).execute()
        except Exception:
            pass
        return

    payload = payload_fn()
    payload.update({"dedupe_key": dedupe_key, "status": "neu", "auto_detected": True, "auto_resolved": False, "ignored": False})
    try:
        supabase.table(TASK_TABLE).insert(payload).execute()
    except Exception:
        pass


def _detect_missing_product_data() -> None:
    try:
        products = supabase.table(PRODUCT_TABLE).select("*").execute().data or []
    except Exception:
        products = []

    incomplete = [p for p in products if p.get("status") not in ("archived", "rejected") and missing_required_fields(p)]

    def _build():
        return {
            "title": f"{len(incomplete)} Affiliate-Produkt(e) mit fehlenden Pflichtfeldern",
            "category": "affiliate",
            "source": "affiliate",
            "priority": "mittel",
            "reason": "Produkte mit fehlenden Pflichtfeldern (Titel/Link/Marke/Beschreibung/Bild/Kategorie) sollten vor Freigabe vervollständigt werden.",
            "data_used": f"vt_affiliate_products: {len(incomplete)} von {len(products)} Produkten unvollständig.",
            "impact_if_ignored": "Unvollständige Produktdarstellung gegenüber Nutzern, geringere Datenqualität im Ranking.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_task("affiliate_intelligence_missing_data", len(incomplete) > 0, _build)


def _detect_possible_duplicates() -> None:
    try:
        open_candidates = (
            supabase.table(DUPLICATE_TABLE).select("id").eq("status", "moegliches_duplikat").execute().data or []
        )
    except Exception:
        open_candidates = []

    def _build():
        return {
            "title": f"{len(open_candidates)} mögliche Produkt-Duplikate zu prüfen",
            "category": "affiliate",
            "source": "affiliate",
            "priority": "niedrig",
            "reason": "Automatische Dublettenerkennung hat übereinstimmende Kernmerkmale zwischen Produkten gefunden.",
            "data_used": f"vt_affiliate_duplicate_candidates: {len(open_candidates)} offene Kandidat(en).",
            "impact_if_ignored": "Doppelte Produkte könnten Nutzern mehrfach empfohlen werden.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_task("affiliate_intelligence_possible_duplicates", len(open_candidates) > 0, _build)


def _detect_revenue_drop() -> None:
    this_week = metrics.get_affiliate_revenue_by_category(days=7)
    two_weeks = metrics.get_affiliate_revenue_by_category(days=14)
    if not two_weeks:
        return

    today = date.today()
    week_ago = today - timedelta(days=7)

    dropped_categories = []
    for category, revenue_two_weeks in two_weeks.items():
        revenue_this_week = this_week.get(category, 0.0)
        revenue_previous_week = max(revenue_two_weeks - revenue_this_week, 0.0)
        if revenue_previous_week <= 0:
            continue
        change_pct = (revenue_this_week - revenue_previous_week) / revenue_previous_week * 100
        if change_pct <= -REVENUE_DROP_THRESHOLD_PCT:
            dropped_categories.append((category, round(change_pct)))

    def _build():
        names = ", ".join(f"{c} ({p}%)" for c, p in dropped_categories)
        return {
            "title": f"Ungewöhnlicher Provisionseinbruch in {len(dropped_categories)} Kategorie(n)",
            "category": "affiliate",
            "source": "affiliate",
            "priority": "hoch",
            "reason": f"Affiliate-Umsatz ist in folgenden Kategorien um mindestens {REVENUE_DROP_THRESHOLD_PCT}% gegenüber der Vorwoche gesunken: {names}.",
            "data_used": "vt_affiliate_events (event_type='conversion'), Zeitfenster-Vergleich (7 vs. 7 Tage) je Kategorie.",
            "impact_if_ignored": "Anhaltender Umsatzrückgang ohne bekannte Ursache.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_task(f"affiliate_intelligence_revenue_drop_{week_ago.isoformat()}", len(dropped_categories) > 0, _build)


def run_affiliate_intelligence_detection() -> None:
    _detect_missing_product_data()
    _detect_possible_duplicates()
    _detect_revenue_drop()
