"""Product Health (VitalTwin Enterprise, Founder Operating System,
Submodule F — Affiliate Intelligence).

Computed fresh on every call from fields that already exist on
`vt_affiliate_products` (`link_status`, `end_date`, `image_url`,
`description`, `category_id`, `region`, `language`) plus the blacklist —
no new "health" table, no cached snapshot (same "compute on read"
philosophy as every other Founder-OS module).
"""

from __future__ import annotations

from datetime import date

HealthStatus = str  # "healthy" | "warning" | "critical" | "paused" | "unknown"


def compute_product_health(product: dict, *, blacklisted: bool) -> dict:
    """Returns `{"status": ..., "reasons": [...]}` — always explains why."""
    reasons: list[str] = []

    if product.get("status") == "paused":
        return {"status": "paused", "reasons": ["Produkt ist manuell pausiert."]}

    if blacklisted:
        reasons.append("Produkt/Marke/Partner/Kategorie steht auf der Blacklist.")

    if product.get("link_status") == "broken":
        reasons.append("Affiliate-Link ist defekt.")
    elif product.get("link_status") == "unchecked":
        reasons.append("Link wurde noch nie geprüft.")

    end_date = product.get("end_date")
    if end_date:
        try:
            if date.fromisoformat(str(end_date)) < date.today():
                reasons.append("Angebot ist abgelaufen.")
        except ValueError:
            pass

    if product.get("availability") == "out_of_stock":
        reasons.append("Produkt ist laut letzter Prüfung nicht verfügbar.")

    if not product.get("image_url"):
        reasons.append("Kein Produktbild hinterlegt.")
    if not product.get("description"):
        reasons.append("Keine Beschreibung hinterlegt.")
    if not product.get("category_id"):
        reasons.append("Keine Kategorie zugeordnet.")
    if not product.get("region"):
        reasons.append("Keine Region hinterlegt.")

    critical_reasons = {"Produkt/Marke/Partner/Kategorie steht auf der Blacklist.", "Affiliate-Link ist defekt.", "Angebot ist abgelaufen.", "Produkt ist laut letzter Prüfung nicht verfügbar."}
    has_critical = any(r in critical_reasons for r in reasons)

    if not reasons:
        return {"status": "healthy", "reasons": ["Alle geprüften Kriterien erfüllt."]}
    if has_critical:
        return {"status": "critical", "reasons": reasons}
    if len(reasons) >= 2:
        return {"status": "warning", "reasons": reasons}
    return {"status": "warning", "reasons": reasons}
