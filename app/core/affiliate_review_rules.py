"""Rule-based product review + Approval Assistant grouping (VitalTwin
Enterprise, Founder Operating System, Submodule F — Affiliate
Intelligence).

**Deterministic, auditable, zero-cost.** All objective checks (sensitive
category, blacklist, missing fields, expired, broken link, duplicate) are
plain rule matches — no LLM call. An LLM is only ever used for an
*optional*, on-demand, per-product deeper explanation
(`routers/founder_affiliate_intelligence.py::ai_review_product`, reusing
the existing `services/ai_provider.py` abstraction) — never for the
mandatory safety checks below, and never automatically for every import
(cost-conscious, matches the Business Coach's "ask" pattern).

**Sensitive products are never auto-approved.** Any match against
`SENSITIVE_CATEGORY_NAMES`/`HEALTH_CLAIM_KEYWORDS`/the blacklist routes a
product to `einzelpruefung` (manual review) — this function only ever
*proposes*, never activates anything (see module docstring in
`routers/founder_affiliate_intelligence.py`).
"""

from __future__ import annotations

from datetime import date

SENSITIVE_CATEGORY_NAMES = {
    "nahrungsergänzung", "nahrungsergaenzung", "supplements",
    "cgm-zubehör", "cgm-zubehoer", "cgm zubehör",
    "blutdruckgeräte", "blutdruckgeraete", "blutdruck",
    "medizingeräte", "medizingeraete",
}

HEALTH_CLAIM_KEYWORDS = {
    "heilt", "heilung", "garantiert", "garantierte wirkung", "verschreibungspflichtig",
    "diagnose", "diagnostiziert", "therapiert", "medikament", "arznei", "rezeptpflichtig",
    "100% wirksam", "wissenschaftlich bewiesen heilt", "kein arztbesuch nötig",
    "ersetzt medikamente", "heilversprechen", "abnehmgarantie", "schlafheilung",
}

REQUIRED_FIELDS = ("title", "affiliate_url", "brand", "description", "image_url", "category_id")


def is_sensitive_category(category_name: str | None) -> bool:
    if not category_name:
        return False
    return category_name.strip().lower() in SENSITIVE_CATEGORY_NAMES


def find_health_claim_keywords(text: str | None) -> list[str]:
    if not text:
        return []
    lowered = text.lower()
    return [kw for kw in HEALTH_CLAIM_KEYWORDS if kw in lowered]


def missing_required_fields(product: dict) -> list[str]:
    return [field for field in REQUIRED_FIELDS if not product.get(field)]


def review_product_rule_based(product: dict, *, category_name: str | None, blacklisted: bool, has_duplicate_candidate: bool) -> dict:
    """Returns the Approval-Assistant bucket + reasoning for one product.
    Buckets (exactly the 8 named in the spec):
    sammelfreigabe, einzelpruefung, automatisch_abgelehnt,
    daten_unvollstaendig, moeglicher_regelverstoss, moegliches_duplikat,
    link_defekt, angebot_abgelaufen.
    """
    if blacklisted:
        return {
            "bucket": "automatisch_abgelehnt", "confidence": "hoch",
            "reasons": ["Produkt/Marke/Partner/Kategorie steht auf der Blacklist."],
            "risks": ["Keine — Blacklist-Ausschluss ist eine bewusste Gründerregel."],
        }

    if product.get("link_status") == "broken":
        return {"bucket": "link_defekt", "confidence": "hoch", "reasons": ["Affiliate-Link ist aktuell defekt."], "risks": ["Nutzer würden auf einen toten Link klicken."]}

    end_date = product.get("end_date")
    if end_date:
        try:
            if date.fromisoformat(str(end_date)) < date.today():
                return {"bucket": "angebot_abgelaufen", "confidence": "hoch", "reasons": [f"end_date ({end_date}) liegt in der Vergangenheit."], "risks": ["Veraltetes Angebot würde beworben."]}
        except ValueError:
            pass
    if has_duplicate_candidate:
        return {"bucket": "moegliches_duplikat", "confidence": "mittel", "reasons": ["Ein oder mehrere bestehende Produkte stimmen in Kernmerkmalen überein."], "risks": ["Doppelte Empfehlung desselben Produkts."]}

    missing = missing_required_fields(product)
    if missing:
        return {"bucket": "daten_unvollstaendig", "confidence": "hoch", "reasons": [f"Fehlende Felder: {', '.join(missing)}."], "risks": ["Unvollständige Darstellung gegenüber Nutzern."]}

    health_claims = find_health_claim_keywords(product.get("description"))
    if health_claims:
        return {
            "bucket": "moeglicher_regelverstoss", "confidence": "mittel",
            "reasons": [f"Mögliche Heilversprechen/gesundheitsbezogene Aussagen erkannt: {', '.join(health_claims)}."],
            "risks": ["Rechtliches/Vertrauensrisiko bei unbelegten Gesundheitsaussagen — erfordert Einzelprüfung, siehe Constitution."],
        }

    if is_sensitive_category(category_name):
        return {
            "bucket": "einzelpruefung", "confidence": "mittel",
            "reasons": [f"Sensible Kategorie ({category_name}) — keine automatische Freigabe ohne dokumentierte Gründerregel."],
            "risks": ["Erhöhtes Vertrauens-/Gesundheitsrisiko bei Kategorie-Fehleinschätzung."],
        }

    return {
        "bucket": "sammelfreigabe", "confidence": "hoch",
        "reasons": ["Alle geprüften Regeln erfüllt: keine Blacklist, kein defekter Link, kein abgelaufenes Angebot, kein Duplikat, vollständige Daten, keine sensible Kategorie/Aussage."],
        "risks": ["Gering — Endgültige Freigabe erfolgt weiterhin im Smart Approval Center durch den Gründer."],
    }


def summarize_approval_assistant(reviews: list[dict]) -> str:
    """Builds the exact style of summary requested in the spec — a plain
    template over real counts, never an LLM call."""
    total = len(reviews)
    if total == 0:
        return "Keine neuen Produkte zur Prüfung."

    counts: dict[str, int] = {}
    for r in reviews:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1

    lines = [f"{total} neue Produkte wurden importiert."]
    if counts.get("sammelfreigabe"):
        lines.append(f"{counts['sammelfreigabe']} erfüllen die aktuell freigegebenen Regeln.")
    if counts.get("moeglicher_regelverstoss"):
        lines.append(f"{counts['moeglicher_regelverstoss']} benötigen wegen gesundheitsbezogener Aussagen oder sensibler Kategorie eine Einzelprüfung.")
    if counts.get("daten_unvollstaendig"):
        lines.append(f"{counts['daten_unvollstaendig']} besitzen unvollständige Daten.")
    if counts.get("link_defekt"):
        lines.append(f"{counts['link_defekt']} Link(s) sind defekt.")
    if counts.get("moegliches_duplikat"):
        lines.append(f"{counts['moegliches_duplikat']} könnten Duplikate sein.")
    if counts.get("angebot_abgelaufen"):
        lines.append(f"{counts['angebot_abgelaufen']} Angebot(e) sind bereits abgelaufen.")
    if counts.get("automatisch_abgelehnt"):
        lines.append(f"{counts['automatisch_abgelehnt']} wurden wegen Blacklist-Treffer automatisch abgelehnt.")
    return " ".join(lines)
