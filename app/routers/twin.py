from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core.supabase import supabase
from .users import get_email_by_token, is_premium_by_email

router = APIRouter()

MARKER_TABLE = "vt_marker_reference"
CALC_TABLE = "vt_twin_calculations"

DEFAULT_MARKER_CONFIG: dict[str, dict[str, Any]] = {
    "hba1c": {
        "lower_bound": 5.0,
        "upper_bound": 5.6,
        "penalty_below": 0.0,
        "penalty_above": 2.8,
        "unit": "%",
        "target_min": 5.0,
        "target_max": 5.6,
        "warn_min": 4.6,
        "warn_max": 6.4,
        "source_name": "ADA Standards of Care",
        "source_url": "https://diabetesjournals.org/care/issue/47/Supplement_1",
        "evidence_level": "hoch",
        "population_note": "Erwachsene ohne Schwangerschaft",
        "recommendation": "HbA1c optimieren: Fokus auf stabile Blutzuckerwerte.",
    },
    "crp": {
        "lower_bound": 0.0,
        "upper_bound": 1.0,
        "penalty_below": 0.0,
        "penalty_above": 3.5,
        "unit": "mg/L",
        "target_min": 0.0,
        "target_max": 1.0,
        "warn_min": 0.0,
        "warn_max": 3.0,
        "source_name": "AHA/CDC Risikoklassifikation hs-CRP",
        "source_url": "https://www.ahajournals.org",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, kardiovaskuläre Risikoeinschätzung",
        "recommendation": "Entzündungsmanagement verbessern (Ernährung, Schlaf, Stress).",
    },
    "vitamin_d": {
        "lower_bound": 30.0,
        "upper_bound": 60.0,
        "penalty_below": 0.6,
        "penalty_above": 0.0,
        "unit": "ng/mL",
        "target_min": 30.0,
        "target_max": 50.0,
        "warn_min": 20.0,
        "warn_max": 80.0,
        "source_name": "Endocrine Society Guideline",
        "source_url": "https://www.endocrine.org",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, Serum 25(OH)D",
        "recommendation": "Vitamin-D-Status regelmäßig kontrollieren und optimieren.",
    },
    "apob": {
        "lower_bound": 0.0,
        "upper_bound": 90.0,
        "penalty_below": 0.0,
        "penalty_above": 0.4,
        "unit": "mg/dL",
        "target_min": 0.0,
        "target_max": 80.0,
        "warn_min": 0.0,
        "warn_max": 110.0,
        "source_name": "ESC/EAS Dyslipidämie-Leitlinie",
        "source_url": "https://www.escardio.org",
        "evidence_level": "hoch",
        "population_note": "Erwachsene, Prävention",
        "recommendation": "ApoB senken durch Lebensstil und ärztlich begleitete Strategie.",
    },
}


class MarkerBreakdown(BaseModel):
    marker: str
    value: float
    contribution: float


class MarkerReferenceResponse(BaseModel):
    marker: str
    unit: str
    target_min: float | None = None
    target_max: float | None = None
    warn_min: float | None = None
    warn_max: float | None = None
    source_name: str
    source_url: str
    evidence_level: str
    population_note: str

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
            .select("marker,lower_bound,upper_bound,penalty_below,penalty_above,unit,target_min,target_max,warn_min,warn_max,source_name,source_url,evidence_level,population_note,recommendation")
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
                "unit": str(row.get("unit", "")),
                "target_min": row.get("target_min"),
                "target_max": row.get("target_max"),
                "warn_min": row.get("warn_min"),
                "warn_max": row.get("warn_max"),
                "source_name": str(row.get("source_name", "")),
                "source_url": str(row.get("source_url", "")),
                "evidence_level": str(row.get("evidence_level", "orientierend")),
                "population_note": str(row.get("population_note", "Erwachsene")),
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
        recommendations.append("Marker sind stabil. Werte regelmäßig weiter tracken.")

    return recommendations


def _store_calculation(
    email: str | None,
    data: TwinInput,
    result: dict[str, Any],
    marker_breakdown: list[dict[str, Any]],
) -> None:
    if not email:
        return

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
    except Exception as exc:
        # Keep calculate endpoint stable even if persistence is temporarily unavailable.
        print(f"Failed to store calculation history: {exc}")
        return


def _build_marker_references(config: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for marker in sorted(config.keys()):
        ref = config[marker]
        references.append(
            MarkerReferenceResponse(
                marker=marker,
                unit=str(ref.get("unit", "")),
                target_min=ref.get("target_min"),
                target_max=ref.get("target_max"),
                warn_min=ref.get("warn_min"),
                warn_max=ref.get("warn_max"),
                source_name=str(ref.get("source_name", "Unbekannte Quelle")),
                source_url=str(ref.get("source_url", "")),
                evidence_level=str(ref.get("evidence_level", "orientierend")),
                population_note=str(ref.get("population_note", "Erwachsene")),
            ).model_dump()
        )
    return references


def _has_existing_calculation(email: str) -> bool:
    try:
        response = (
            supabase.table(CALC_TABLE)
            .select("id")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return bool(response.data)
    except Exception:
        # If storage is unavailable, do not block calculation solely due to lookup issues.
        return False

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

    email = get_email_by_token(data.token)
    is_premium = bool(email) and is_premium_by_email(email)

    if email and not is_premium and _has_existing_calculation(email):
        raise HTTPException(
            status_code=403,
            detail="Starter enthält eine einmalige Twin-Berechnung. Aktiviere den Beta-Zugang für unbegrenzte Simulationen.",
        )

    starter_recommendations = [
        "Achte auf Schlaf, Stressmanagement und regelmäßige Bewegung.",
        "Kontrolliere deine Marker regelmäßig für bessere Vergleichbarkeit.",
        "Für personalisierte Szenarien und Verlauf aktiviere den Beta-Zugang.",
    ]

    result = {
        "biologisches_alter": round(bio_age, 1),
        "differenz": round(bio_age - data.age, 1),
        "scenarios": {
            "aktuell": round(bio_age, 1),
            "optimiert": round(bio_age - 5.5, 1) if is_premium else round(bio_age, 1),
            "aggressiv": round(bio_age - 9.0, 1) if is_premium else round(bio_age, 1),
        },
        "methodik": {
            "typ": "Wellness-Orientierung",
            "hinweis": (
                "Kein medizinisches Produkt. Keine Diagnose oder Therapieempfehlung."
                if is_premium
                else "Starter-Modus: Basis-Auswertung. Für vollständige Simulationen und Detailquellen aktiviere den Beta-Zugang."
            ),
        },
        "marker_references": _build_marker_references(marker_config) if is_premium else [],
        "empfehlungen": recommendations if is_premium else starter_recommendations,
        "marker_breakdown": marker_breakdown,
    }

    _store_calculation(email, data, result, marker_breakdown)

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

    premium = is_premium_by_email(email)
    if premium:
        query_limit = 10 if limit <= 0 else min(limit, 50)
    else:
        # Starter can still access their latest saved run to avoid empty dashboards.
        query_limit = 1

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


@router.get("/references")
async def references():
    marker_config = _load_marker_config()
    return {
        "items": _build_marker_references(marker_config),
        "methodik": {
            "typ": "Wellness-Orientierung",
            "hinweis": "Die Referenzdaten dienen der Gesundheitsorientierung und ersetzen keine medizinische Diagnostik.",
        },
    }