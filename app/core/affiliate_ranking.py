"""Smart Ranking (VitalTwin Enterprise, Founder Operating System,
Submodule F — Affiliate Intelligence).

**One centrally configured weighting, not scattered across files** (per
spec). Used by the Recommendation Simulator and the Affiliate Intelligence
dashboard to *explain* ranking — it does **not** replace or change the
live sort order in `core/affiliate_engine.py` (which already never sorts
by commission at all, and is production-tested/in active use — changing
it here would risk regressing real user-facing recommendations, which the
spec explicitly says not to replace).
"""

from __future__ import annotations

# Centrally configurable — the only place these numbers live.
RANKING_WEIGHTS = {
    "quality": 0.35,
    "relevance": 0.25,
    "feedback": 0.15,
    "conversion": 0.10,
    "recency_availability": 0.10,
    "commission": 0.05,
}


def _quality_component(product: dict) -> float:
    """Data completeness — the same fields checked in
    `core/affiliate_review_rules.py::missing_required_fields`."""
    fields = ("title", "affiliate_url", "brand", "description", "image_url", "category_id")
    present = sum(1 for f in fields if product.get(f))
    return present / len(fields)


def _relevance_component(product: dict, context_category_id: str | None) -> float:
    if context_category_id is None:
        return 0.5  # Neutral — no context to judge relevance against.
    return 1.0 if str(product.get("category_id")) == str(context_category_id) else 0.0


def _feedback_component(product: dict) -> float:
    rating = product.get("rating")
    if rating is None:
        return 0.5  # Neutral — no feedback signal available yet.
    return max(0.0, min(1.0, float(rating) / 5.0))


def _conversion_component(impressions: int, conversions: int) -> float:
    if impressions <= 0:
        return 0.5  # Neutral — not enough traffic to judge yet.
    return max(0.0, min(1.0, conversions / impressions * 10))  # 10% CR -> 1.0, scaled.


def _recency_availability_component(product: dict) -> float:
    score = 1.0
    if product.get("availability") == "out_of_stock":
        score -= 0.5
    if product.get("link_status") != "ok":
        score -= 0.5
    return max(0.0, score)


def _commission_component(product: dict) -> float:
    rate = product.get("commission_rate")
    if rate is None:
        return 0.0
    return max(0.0, min(1.0, float(rate) / 50.0))  # 50% commission -> 1.0 (generous ceiling).


def compute_product_score(
    product: dict, *, context_category_id: str | None = None, impressions: int = 0, conversions: int = 0
) -> dict:
    """Returns `{"score": 0..1, "breakdown": {...}, "explanation": [...]}`
    — every factor is named so the ranking is always explainable, per
    spec ("Das Ranking muss erklärbar sein")."""
    components = {
        "quality": _quality_component(product),
        "relevance": _relevance_component(product, context_category_id),
        "feedback": _feedback_component(product),
        "conversion": _conversion_component(impressions, conversions),
        "recency_availability": _recency_availability_component(product),
        "commission": _commission_component(product),
    }
    score = sum(components[key] * weight for key, weight in RANKING_WEIGHTS.items())

    explanation = [
        f"{key} ({RANKING_WEIGHTS[key] * 100:.0f}%): {value:.2f}" for key, value in components.items()
    ]
    return {"score": round(score, 4), "breakdown": components, "weights": RANKING_WEIGHTS, "explanation": explanation}
