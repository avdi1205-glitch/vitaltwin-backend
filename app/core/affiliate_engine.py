"""Affiliate recommendation engine — rule-based product eligibility.

VitalTwin Enterprise Release — Affiliate Intelligence & Management Platform.

**No AI/LLM call happens in this module.** Product "recommendation" here is
a deterministic, auditable rule filter over admin-curated data — never a
free-text generation. This matches the mission statement: "Die KI darf
ausschliesslich Produkte empfehlen, die vom Administrator geprueft und
freigegeben wurden" — enforced here by construction, not by prompting.

Eligibility rules (all must hold):

1. `status` is `approved` or `active` — every other status (draft,
   in_review, paused, expired, archived) is excluded, no exceptions.
2. Not expired: `start_date` is null or `<= today`, `end_date` is null or
   `>= today`.
3. `link_status != "broken"` — see `affiliate_link_checker.py`.
4. Not blacklisted — by product id, brand, partner id, or category name.
5. Respects the user's own preferences (`vt_affiliate_user_prefs`):
   `affiliate_enabled = false` -> no recommendations at all; hidden
   categories/products are excluded.

Every call to `get_recommendations_for_user` writes one row per returned
product to `vt_affiliate_recommendation_log` recording *why* it was chosen
(`rule_applied`) — the "KI Transparenz" requirement.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from .supabase import supabase

PRODUCT_TABLE = "vt_affiliate_products"
CATEGORY_TABLE = "vt_affiliate_categories"
BLACKLIST_TABLE = "vt_affiliate_blacklist"
USER_PREFS_TABLE = "vt_affiliate_user_prefs"
RECOMMENDATION_LOG_TABLE = "vt_affiliate_recommendation_log"

ELIGIBLE_STATUSES = frozenset({"approved", "active"})


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _fetch_blacklist() -> dict[str, set[str]]:
    """Returns `{"product": {...ids}, "brand": {...}, "partner": {...ids}, "category": {...}}`."""
    result: dict[str, set[str]] = {"product": set(), "brand": set(), "partner": set(), "category": set()}
    try:
        rows = supabase.table(BLACKLIST_TABLE).select("entry_type,value").execute().data or []
    except Exception:
        return result
    for row in rows:
        entry_type = row.get("entry_type")
        value = row.get("value")
        if entry_type in result and value:
            result[entry_type].add(value)
    return result


def _is_blacklisted(product: dict, blacklist: dict[str, set[str]]) -> bool:
    if str(product.get("id")) in blacklist["product"]:
        return True
    if product.get("brand") and product["brand"] in blacklist["brand"]:
        return True
    if product.get("partner_id") and str(product["partner_id"]) in blacklist["partner"]:
        return True
    category_name = product.get("_category_name")
    if category_name and category_name in blacklist["category"]:
        return True
    return False


def _is_within_validity_window(product: dict) -> bool:
    today = _today()
    start_date = product.get("start_date")
    end_date = product.get("end_date")
    if start_date and str(start_date) > str(today):
        return False
    if end_date and str(end_date) < str(today):
        return False
    return True


def get_user_prefs(email: str) -> dict:
    """Real user preferences, or the honest defaults (`affiliate_enabled=True`,
    nothing hidden) if the user has never changed anything."""
    try:
        rows = supabase.table(USER_PREFS_TABLE).select("*").eq("email", email).limit(1).execute().data or []
    except Exception:
        rows = []
    if rows:
        return rows[0]
    return {"email": email, "affiliate_enabled": True, "hidden_categories": [], "hidden_products": []}


def _category_name_map() -> dict[str, str]:
    try:
        rows = supabase.table(CATEGORY_TABLE).select("id,name").execute().data or []
    except Exception:
        return {}
    return {str(row["id"]): row["name"] for row in rows if row.get("id")}


def get_eligible_products(*, category: str | None = None, limit: int = 20) -> list[dict]:
    """Rule-filtered products — ignores user-specific hiding (see
    `get_recommendations_for_user` for the user-aware version)."""
    try:
        query = supabase.table(PRODUCT_TABLE).select("*")
        if category:
            query = query.eq("category_id", category)
        rows = query.execute().data or []
    except Exception:
        return []

    category_names = _category_name_map()
    for product in rows:
        product["_category_name"] = category_names.get(str(product.get("category_id")))

    blacklist = _fetch_blacklist()
    eligible = [
        product
        for product in rows
        if product.get("status") in ELIGIBLE_STATUSES
        and product.get("link_status") != "broken"
        and _is_within_validity_window(product)
        and not _is_blacklisted(product, blacklist)
    ]
    eligible.sort(
        key=lambda p: (not p.get("pinned", False), -(p.get("priority") or 0), -(p.get("rating") or 0)),
    )
    return eligible[:limit]


def _log_recommendation(*, email: str | None, product: dict, category: str | None, rule_applied: str) -> None:
    try:
        supabase.table(RECOMMENDATION_LOG_TABLE).insert(
            {
                "email": email,
                "product_id": product.get("id"),
                "category": category,
                "rule_applied": rule_applied,
                "reason": (
                    f"Status={product.get('status')}, pinned={product.get('pinned')}, "
                    f"priority={product.get('priority')}, link_status={product.get('link_status')}"
                ),
                "context": {"category": category},
            }
        ).execute()
    except Exception:
        pass


def get_recommendations_for_user(email: str, *, category: str | None = None, limit: int = 10) -> list[dict]:
    """The only function that should ever be called to decide what a real
    user sees. Wraps `get_eligible_products` with the user's own
    preferences and writes an auditable transparency log entry per
    returned product."""
    prefs = get_user_prefs(email)
    if not prefs.get("affiliate_enabled", True):
        return []

    hidden_categories = set(prefs.get("hidden_categories") or [])
    hidden_products = set(str(p) for p in (prefs.get("hidden_products") or []))

    candidates = get_eligible_products(category=category, limit=limit * 3)
    results: list[dict] = []
    for product in candidates:
        if str(product.get("id")) in hidden_products:
            continue
        if product.get("category_id") and str(product["category_id"]) in hidden_categories:
            continue
        results.append(product)
        if len(results) >= limit:
            break

    for product in results:
        _log_recommendation(
            email=email,
            product=product,
            category=category,
            rule_applied="eligible_status_not_expired_not_blacklisted_not_hidden",
        )
    return results
