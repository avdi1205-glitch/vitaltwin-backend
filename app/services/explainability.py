"""Explainability builder for the Recommendation Loop.

Twin Intelligence Core — Etappe 4 §7.

Turns a persisted `vt_recommendations` row (plus its stored `explanation`
JSON, written at creation time by `routers/recommendations.py`) into the
structured "Warum?" answer. Never fabricates a reason that wasn't actually
recorded when the recommendation was generated, and never surfaces internal
system prompts (there are none to leak — recommendations are purely
rule-based in this etappe, see `recommendation_rules.py`).
"""

from __future__ import annotations

DISCLAIMER = (
    "Diese Empfehlung basiert auf einer nachvollziehbaren Regel und deinen eigenen Daten "
    "— sie ist keine medizinische Notwendigkeit und ersetzt keine ärztliche Beratung."
)


def build_explanation_response(recommendation: dict[str, object]) -> dict[str, object]:
    explanation = recommendation.get("explanation")
    explanation = explanation if isinstance(explanation, dict) else {}

    rule_name = explanation.get("rule_name")
    return {
        "rule_name": rule_name,
        "data_used": explanation.get("data_used", []),
        "period_days": explanation.get("period_days"),
        "data_points": explanation.get("data_points"),
        "data_quality": explanation.get("data_quality"),
        "confidence": recommendation.get("confidence"),
        "goal_id": recommendation.get("goal_id"),
        "habit_id": recommendation.get("habit_id"),
        "expected_benefit": explanation.get("expected_benefit"),
        # Every recommendation in this etappe is rule-based (source_type is
        # always "rule_based") — never claim it's a "personal insight" the
        # Twin discovered unless a later etappe's pattern/insight system
        # actually produced it.
        "type": "allgemeine Regel" if rule_name else "unbekannt",
        "disclaimer": DISCLAIMER,
    }
