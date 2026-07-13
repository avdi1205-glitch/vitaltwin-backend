from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core.supabase import supabase
from .users import get_email_by_token

router = APIRouter()

MARKER_TABLE = "vt_marker_reference"
CALC_TABLE = "vt_twin_calculations"

DEFAULT_MARKER_CONFIG: dict[str, dict[str, Any]] = {
    "hba1c": {
        "lower_bound": 5.0,
        "upper_bound": 5.0,
        "penalty_below": 0.0,
        "penalty_above": 2.8,
        "recommendation": "HbA1c optimieren: Fokus auf stabile Blutzuckerwerte.",
    },
    "crp": {
        "lower_bound": 1.0,
        "upper_bound": 1.0,
        "penalty_below": 0.0,
        "penalty_above": 3.5,
        "recommendation": "Entzuendungsmanagement verbessern (Ernaehrung, Schlaf, Stress).",
    },
    "vitamin_d": {
        "lower_bound": 40.0,
        "upper_bound": 120.0,
        "penalty_below": 0.6,
        "penalty_above": 0.0,
        "recommendation": "Vitamin-D-Status regelmaessig kontrollieren und optimieren.",
    },
    "apob": {
        "lower_bound": 70.0,
        "upper_bound": 70.0,
        "penalty_below": 0.0,
        "penalty_above": 0.4,
        "recommendation": "ApoB senken durch Lebensstil und aerztlich begleitete Strategie.",
    },
}


class MarkerBreakdown(BaseModel):
    marker: str
    value: float
    contribution: float

class TwinInput(BaseModel):
    age: int
    gender: str
    hba1c: float = 5.5
    crp: float = 1.2
    vitamin_d: float = 40.0
    apob: float = 80.0
    token: str | None = None


def _load_marker_config() -> dict[str, dict[str, Any]]:
    try:
        response = (
            supabase.table(MARKER_TABLE)
            .select("marker,lower_bound,upper_bound,penalty_below,penalty_above,recommendation")
            .execute()
        )
        rows = response.data or []
        if not rows:
            return DEFAULT_MARKER_CONFIG

        config: dict[str, dict[str, Any]] = {}
        for row in rows:
            marker = str(row.get("marker", "")).strip().lower()
            if not marker:
                continue
            config[marker] = {
                "lower_bound": float(row.get("lower_bound", 0.0)),
                "upper_bound": float(row.get("upper_bound", 0.0)),
                "penalty_below": float(row.get("penalty_below", 0.0)),
                "penalty_above": float(row.get("penalty_above", 0.0)),
                "recommendation": row.get("recommendation") or DEFAULT_MARKER_CONFIG.get(marker, {}).get("recommendation", "Marker optimieren."),
            }

        for marker, defaults in DEFAULT_MARKER_CONFIG.items():
            config.setdefault(marker, defaults)

        return config
    except Exception:
        return DEFAULT_MARKER_CONFIG


def _calc_marker_contribution(value: float, marker_config: dict[str, Any]) -> float:
    lower = float(marker_config.get("lower_bound", 0.0))
    upper = float(marker_config.get("upper_bound", 0.0))
    below = float(marker_config.get("penalty_below", 0.0))
    above = float(marker_config.get("penalty_above", 0.0))

    contribution = 0.0
    if value < lower:
        contribution += (lower - value) * below
    if value > upper:
        contribution += (value - upper) * above
    return round(contribution, 3)


def _build_recommendations(marker_breakdown: list[dict[str, Any]], config: dict[str, dict[str, Any]]) -> list[str]:
    sorted_markers = sorted(marker_breakdown, key=lambda item: item["contribution"], reverse=True)
    recommendations: list[str] = []

    for item in sorted_markers:
        if item["contribution"] <= 0:
            continue
        marker = item["marker"]
        recommendation = str(config.get(marker, {}).get("recommendation", "Marker optimieren."))
        recommendations.append(recommendation)
        if len(recommendations) >= 3:
            break

    if not recommendations:
        recommendations.append("Marker sind stabil. Werte regelmaessig weiter tracken.")

    return recommendations


def _store_calculation(
    email: str | None,
    data: TwinInput,
    result: dict[str, Any],
    marker_breakdown: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    if not email:
        return False, "Kein User fuer Speicherung (Token fehlt oder ungueltig)."

    try:
        supabase.table(CALC_TABLE).insert(
            {
                "email": email,
                "age": data.age,
                "gender": data.gender,
                "hba1c": data.hba1c,
                "crp": data.crp,
                "vitamin_d": data.vitamin_d,
                "apob": data.apob,
                "biologisches_alter": result["biologisches_alter"],
                "differenz": result["differenz"],
                "scenarios": result["scenarios"],
                "marker_breakdown": marker_breakdown,
            }
        ).execute()
        return True, None
    except Exception as exc:
        # Keep calculate endpoint stable even if persistence is temporarily unavailable.
        return False, str(exc)

@router.post("/calculate")
async def calculate(data: TwinInput):
    marker_config = _load_marker_config()
    values = {
        "hba1c": data.hba1c,
        "crp": data.crp,
        "vitamin_d": data.vitamin_d,
        "apob": data.apob,
    }

    marker_breakdown: list[dict[str, Any]] = []
    for marker, value in values.items():
        contribution = _calc_marker_contribution(float(value), marker_config.get(marker, DEFAULT_MARKER_CONFIG[marker]))
        marker_breakdown.append(
            MarkerBreakdown(
                marker=marker,
                value=float(value),
                contribution=contribution,
            ).model_dump()
        )

    bio_age = float(data.age) + sum(item["contribution"] for item in marker_breakdown)
    recommendations = _build_recommendations(marker_breakdown, marker_config)

    result = {
        "biologisches_alter": round(bio_age, 1),
        "differenz": round(bio_age - data.age, 1),
        "scenarios": {
            "aktuell": round(bio_age, 1),
            "optimiert": round(bio_age - 5.5, 1),
            "aggressiv": round(bio_age - 9.0, 1),
        },
        "empfehlungen": recommendations,
        "marker_breakdown": marker_breakdown,
    }

    email = get_email_by_token(data.token)
    saved, save_error = _store_calculation(email, data, result, marker_breakdown)

    result["persistence"] = {
        "saved": saved,
        "email": email,
        "error": save_error,
    }

    return result


@router.get("/history")
async def history(
    authorization: str | None = Header(default=None),
    limit: int = 10,
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    token = authorization.split(" ", 1)[1].strip()
    email = get_email_by_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Session abgelaufen")

    query_limit = 10 if limit <= 0 else min(limit, 50)

    try:
        response = (
            supabase.table(CALC_TABLE)
            .select("id,created_at,age,gender,hba1c,crp,vitamin_d,apob,biologisches_alter,differenz,scenarios,marker_breakdown")
            .eq("email", email)
            .order("created_at", desc=True)
            .limit(query_limit)
            .execute()
        )
        return {"items": response.data or []}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"History query failed: {str(exc)}")