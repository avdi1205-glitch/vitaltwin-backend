"""Founder Autopilot — Priority Engine & Founder Attention Score
(VitalTwin Enterprise, Founder Operating System, Submodule J).

Both are pure, deterministic scoring functions over already-computed
fields from other modules (severity, category, reversibility, deadlines)
— never a second data source, never an LLM call (per spec: "Regelbasierte
Logik zuerst").
"""

from __future__ import annotations

PRIORITY_LEVELS = ("kritisch", "hoch", "mittel", "niedrig", "information")

# Weighted per the spec's 12 factors — safety/security and data-loss risk
# dominate, revenue impact alone is deliberately capped low (per spec:
# "Keine Priorisierung allein nach Umsatz").
_FACTOR_WEIGHTS = {
    "security_risk": 3.0, "data_loss_risk": 3.0, "payment_failure": 2.5, "system_outage": 2.5,
    "legal_risk": 2.5, "user_trust_risk": 2.0, "revenue_impact": 1.0, "deadline_pressure": 1.5,
    "affected_user_count": 1.0, "effort_inverse": 0.5, "irreversibility": 2.0, "data_quality_risk": 0.5,
}


def compute_priority_score(factors: dict) -> float:
    """`factors` is a dict of factor_name -> 0..1 float (how strongly this
    factor applies). Unknown factors are ignored, missing ones count as
    0. Returns a raw weighted score (not yet bucketed)."""
    return sum(_FACTOR_WEIGHTS.get(name, 0) * max(0.0, min(1.0, value)) for name, value in factors.items())


def score_to_priority(score: float) -> str:
    if score >= 8:
        return "kritisch"
    if score >= 5:
        return "hoch"
    if score >= 2:
        return "mittel"
    if score > 0:
        return "niedrig"
    return "information"


def compute_priority(item: dict) -> str:
    """Convenience wrapper: derives factors from common fields already
    present on Founder-OS items (category, severity, reversible,
    deadline, affected_systems) rather than requiring callers to build
    the factor dict by hand for the common case."""
    factors = {
        "security_risk": 1.0 if item.get("category") in ("sicherheit", "security") else 0.0,
        "data_loss_risk": 1.0 if item.get("irreversible") else 0.0,
        "payment_failure": 1.0 if item.get("category") in ("payments", "zahlungen") else 0.0,
        "system_outage": 1.0 if item.get("category") in ("system_monitoring", "api_monitoring") and item.get("severity") == "kritisch" else 0.0,
        "legal_risk": 1.0 if item.get("category") in ("rechtliches", "datenschutz") else 0.0,
        "user_trust_risk": 0.5 if item.get("category") == "support" else 0.0,
        "revenue_impact": 0.3 if item.get("category") in ("business", "affiliate") else 0.0,
        "deadline_pressure": 1.0 if item.get("deadline") else 0.0,
        "affected_user_count": 0.5 if item.get("affects_many_users") else 0.0,
        "irreversibility": 1.0 if not item.get("reversible", True) else 0.0,
    }
    severity = item.get("severity") or item.get("priority")
    base_bump = {"kritisch": 8.5, "hoch": 3.0, "mittel": 1.0, "niedrig": 0.3}.get(severity, 0.0)
    return score_to_priority(compute_priority_score(factors) + base_bump)


def compute_attention_score(item: dict) -> float:
    """Higher = needs real founder attention now. Combines: risk,
    potential damage, irreversibility, legal relevance, financial
    threshold exceeded, repeated failure, multi-module impact, deadline,
    mandatory manual decision. Deliberately not normalized to 0-100 (no
    fake precision) — only used for relative ranking within one request."""
    score = 0.0
    severity = item.get("severity") or item.get("priority")
    score += {"kritisch": 10.0, "hoch": 6.0, "mittel": 3.0, "niedrig": 1.0}.get(severity, 0.0)
    if not item.get("reversible", True):
        score += 4.0
    if item.get("category") in ("rechtliches", "datenschutz", "sicherheit", "preise", "tarife"):
        score += 5.0
    if item.get("financial_threshold_exceeded"):
        score += 4.0
    if item.get("repeated_failure"):
        score += 3.0
    affected_modules = item.get("affected_modules") or []
    score += min(len(affected_modules), 3) * 1.0
    if item.get("deadline"):
        score += 2.0
    if item.get("requires_approval"):
        score += 2.0
    return score
