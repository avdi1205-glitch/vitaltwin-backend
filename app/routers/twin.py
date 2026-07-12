from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class TwinInput(BaseModel):
    age: int = Field(ge=18, le=100)
    gender: str
    hba1c: float = Field(default=5.5, ge=3.0, le=15.0)
    crp: float = Field(default=1.2, ge=0.0, le=20.0)
    vitamin_d: float = Field(default=40.0, ge=0.0, le=200.0)
    apob: float = Field(default=80.0, ge=20.0, le=250.0)


def _gender_factor(gender: str) -> float:
    normalized = gender.strip().lower().replace("ae", "a").replace("oe", "o").replace("ue", "u")
    if normalized in {"weiblich", "female", "f"}:
        return -0.3
    if normalized in {"mannlich", "maennlich", "male", "m"}:
        return 0.2
    return 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


@router.post("/calculate")
async def calculate(data: TwinInput):
    # Basismodell: Altersdelta durch Biomarker-Last.
    bio_age = float(data.age)
    bio_age += (data.hba1c - 5.0) * 2.8
    bio_age += (data.crp - 1.0) * 3.5
    bio_age += max(0, (40 - data.vitamin_d) * 0.6)
    bio_age += max(0, (data.apob - 70) * 0.4)
    bio_age += _gender_factor(data.gender)

    bio_age = _clamp(bio_age, data.age - 12, data.age + 22)
    delta = bio_age - data.age

    optimized = _clamp(
        bio_age
        - (max(0.0, data.hba1c - 5.2) * 1.8)
        - (max(0.0, data.crp - 0.8) * 2.2)
        - (max(0.0, 55 - data.vitamin_d) * 0.08)
        - (max(0.0, data.apob - 60) * 0.07),
        data.age - 12,
        data.age + 10,
    )
    aggressive = _clamp(optimized - 3.5, data.age - 15, data.age + 8)

    recommendations: list[str] = []
    if data.hba1c > 5.6:
        recommendations.append("Blutzucker verbessern (Ernaehrung, Kraft- und Ausdauertraining)")
    if data.crp > 1.0:
        recommendations.append("Entzuendung senken (Schlaf, Stressmanagement, Omega-3)")
    if data.vitamin_d < 40:
        recommendations.append("Vitamin-D-Status optimieren und regelmaessig kontrollieren")
    if data.apob > 70:
        recommendations.append("ApoB senken (Ballaststoffe, Gewichtsmanagement, ärztliche Ruecksprache)")
    if not recommendations:
        recommendations.append("Stabile Werte: Fokus auf Routinen, Bewegung und Schlafqualitaet")

    health_score = _clamp(100 - (delta * 6), 35, 100)

    return {
        "biologisches_alter": round(bio_age, 1),
        "differenz": round(delta, 1),
        "health_score": round(health_score, 1),
        "scenarios": {
            "aktuell": round(bio_age, 1),
            "optimiert": round(optimized, 1),
            "aggressiv": round(aggressive, 1),
        },
        "empfehlungen": recommendations,
    }
