"""Duplicate detection (VitalTwin Enterprise, Founder Operating System,
Submodule F — Affiliate Intelligence).

Compares a product against every *other* real product already stored in
`vt_affiliate_products` — no fuzzy-matching library, no external service:
plain, explainable, deterministic comparisons on fields that already
exist (`affiliate_url`, normalized `title`, `brand`). Possible duplicates
are never merged/deleted automatically — a candidate row is created for
the founder to confirm or reject (see `routers/founder_affiliate_
intelligence.py`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .supabase import supabase

PRODUCT_TABLE = "vt_affiliate_products"
DUPLICATE_TABLE = "vt_affiliate_duplicate_candidates"


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def find_duplicate_candidates(product: dict, *, existing_products: list[dict] | None = None) -> list[dict]:
    """Returns `[{"product_id": ..., "reason": ...}, ...]` for every other
    product that plausibly matches — never a definitive merge decision."""
    if existing_products is None:
        try:
            existing_products = supabase.table(PRODUCT_TABLE).select("*").execute().data or []
        except Exception:
            return []

    normalized_title = _normalize_title(product.get("title", ""))
    matches: list[dict] = []

    for other in existing_products:
        if str(other.get("id")) == str(product.get("id")):
            continue

        if product.get("affiliate_url") and other.get("affiliate_url") == product.get("affiliate_url"):
            matches.append({"product_id": other["id"], "reason": "Identischer Affiliate-Link."})
            continue

        other_title = _normalize_title(other.get("title", ""))
        same_brand = product.get("brand") and other.get("brand") == product.get("brand")
        if normalized_title and other_title == normalized_title and same_brand:
            matches.append({"product_id": other["id"], "reason": "Identischer normalisierter Titel und identische Marke."})
            continue

        if product.get("external_product_id") and other.get("external_product_id") == product.get("external_product_id"):
            matches.append({"product_id": other["id"], "reason": "Identische externe Produkt-ID."})

    return matches


def create_duplicate_candidates(product_id: str, matches: list[dict]) -> int:
    """Idempotent — the `unique(product_a_id, product_b_id)` constraint
    means re-scanning never creates a second row for the same pair."""
    created = 0
    for match in matches:
        pair = sorted([str(product_id), str(match["product_id"])])
        payload = {
            "product_a_id": pair[0],
            "product_b_id": pair[1],
            "match_reason": match["reason"],
        }
        try:
            existing = (
                supabase.table(DUPLICATE_TABLE)
                .select("id")
                .eq("product_a_id", pair[0])
                .eq("product_b_id", pair[1])
                .limit(1)
                .execute()
                .data
                or []
            )
            if existing:
                continue
            supabase.table(DUPLICATE_TABLE).insert(payload).execute()
            created += 1
        except Exception:
            continue
    return created


def resolve_duplicate_candidate(candidate_id: str, *, status: str, resolved_by: str) -> None:
    try:
        supabase.table(DUPLICATE_TABLE).update(
            {
                "status": status,
                "resolved_by": resolved_by,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", candidate_id).execute()
    except Exception:
        pass
