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
        "recommendations": [
            "HbA1c optimieren: Fokus auf stabile Blutzuckerwerte durch regelmäßige Mahlzeitenrhythmen.",
            "Kohlenhydratqualität verbessern: mehr Ballaststoffe, weniger stark verarbeitete Produkte.",
        ],
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
        "recommendations": [
            "Entzündungsmanagement verbessern: Ernährung, Schlaf und Stress im Blick behalten.",
            "Regelmäßige moderate Bewegung kann helfen, niedriggradige Entzündungswerte zu senken.",
        ],
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
        "recommendations": [
            "Vitamin-D-Status regelmäßig kontrollieren und optimieren.",
            "Zeit im Freien und Sonnenlichtexposition im Alltag einplanen, soweit möglich.",
        ],
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
        "recommendations": [
            "ApoB senken durch Lebensstil und ärztlich begleitete Strategie.",
            "Gesättigte Fette reduzieren, Ballaststoffe und ungesättigte Fette erhöhen.",
        ],
    },
    "fasting_glucose": {
        "lower_bound": 70.0,
        "upper_bound": 99.0,
        "penalty_below": 0.0,
        "penalty_above": 0.05,
        "unit": "mg/dL",
        "target_min": 70.0,
        "target_max": 99.0,
        "warn_min": 60.0,
        "warn_max": 125.0,
        "source_name": "ADA Standards of Care",
        "source_url": "https://diabetesjournals.org/care/issue/47/Supplement_1",
        "evidence_level": "hoch",
        "population_note": "Erwachsene, nüchtern gemessen",
        "recommendations": [
            "Nüchternglukose im Blick behalten: regelmäßige Bewegung nach den Mahlzeiten hilft.",
            "Zuckerhaltige Getränke und schnelle Kohlenhydrate reduzieren.",
        ],
    },
    "hdl": {
        "lower_bound": 45.0,
        "upper_bound": 200.0,
        "penalty_below": 0.05,
        "penalty_above": 0.0,
        "unit": "mg/dL",
        "target_min": 45.0,
        "target_max": None,
        "warn_min": 35.0,
        "warn_max": None,
        "source_name": "ESC/EAS Dyslipidämie-Leitlinie",
        "source_url": "https://www.escardio.org",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, vereinfachter Richtwert (nicht geschlechtsspezifisch differenziert)",
        "recommendations": [
            "HDL-Cholesterin unterstützen: regelmäßige Ausdauerbewegung ist gut untersucht.",
            "Ungesättigte Fette (z. B. Olivenöl, Nüsse, Fisch) bevorzugen.",
        ],
    },
    "triglycerides": {
        "lower_bound": 0.0,
        "upper_bound": 150.0,
        "penalty_below": 0.0,
        "penalty_above": 0.02,
        "unit": "mg/dL",
        "target_min": 0.0,
        "target_max": 150.0,
        "warn_min": 0.0,
        "warn_max": 200.0,
        "source_name": "ESC/EAS Dyslipidämie-Leitlinie",
        "source_url": "https://www.escardio.org",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, nüchtern gemessen",
        "recommendations": [
            "Triglyceride senken: Alkohol und zugesetzten Zucker reduzieren.",
            "Omega-3-reiche Lebensmittel wie fetten Fisch regelmäßig einplanen.",
        ],
    },
    "homocysteine": {
        "lower_bound": 0.0,
        "upper_bound": 10.0,
        "penalty_below": 0.0,
        "penalty_above": 0.15,
        "unit": "µmol/L",
        "target_min": 0.0,
        "target_max": 10.0,
        "warn_min": 0.0,
        "warn_max": 15.0,
        "source_name": "AHA Scientific Statement Homocysteine",
        "source_url": "https://www.ahajournals.org",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, orientierender Richtwert",
        "recommendations": [
            "B-Vitamin-reiche Ernährung (Blattgemüse, Vollkorn, Hülsenfrüchte) im Blick behalten.",
            "Bei dauerhaft erhöhten Werten ärztliche Abklärung der Ursache empfehlenswert.",
        ],
    },
    "tsh": {
        "lower_bound": 0.5,
        "upper_bound": 2.5,
        "penalty_below": 0.3,
        "penalty_above": 0.3,
        "unit": "mIU/L",
        "target_min": 0.5,
        "target_max": 2.5,
        "warn_min": 0.3,
        "warn_max": 4.5,
        "source_name": "Deutsche Gesellschaft für Endokrinologie",
        "source_url": "https://www.endokrinologie.net",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, orientierender Wellness-Richtwert (kein Diagnosewert)",
        "recommendations": [
            "TSH-Wert bei Auffälligkeiten ärztlich einordnen lassen, nicht selbst behandeln.",
            "Regelmäßiger Schlaf-Wach-Rhythmus unterstützt die hormonelle Balance allgemein.",
        ],
    },
    "ferritin": {
        "lower_bound": 30.0,
        "upper_bound": 150.0,
        "penalty_below": 0.02,
        "penalty_above": 0.01,
        "unit": "ng/mL",
        "target_min": 30.0,
        "target_max": 150.0,
        "warn_min": 15.0,
        "warn_max": 300.0,
        "source_name": "WHO Referenzbereiche Eisenstatus",
        "source_url": "https://www.who.int",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, vereinfachter Richtwert",
        "recommendations": [
            "Eisenreiche und eisenhemmende/-fördernde Lebensmittelkombinationen beachten.",
            "Bei stark abweichenden Werten ärztliche Ursachenklärung statt Selbstsupplementierung.",
        ],
    },
    "vitamin_b12": {
        "lower_bound": 400.0,
        "upper_bound": 900.0,
        "penalty_below": 0.01,
        "penalty_above": 0.0,
        "unit": "pg/mL",
        "target_min": 400.0,
        "target_max": 900.0,
        "warn_min": 200.0,
        "warn_max": None,
        "source_name": "Endocrine Society / DGE Referenzwerte",
        "source_url": "https://www.dge.de",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, funktioneller Richtwert oberhalb des reinen Laborminimums",
        "recommendations": [
            "B12-Quellen (bei veganer/vegetarischer Ernährung besonders beachten) regelmäßig einplanen.",
            "Bei dauerhaft niedrigen Werten ärztliche Abklärung empfehlenswert.",
        ],
    },
    "omega3_index": {
        "lower_bound": 8.0,
        "upper_bound": 20.0,
        "penalty_below": 0.15,
        "penalty_above": 0.0,
        "unit": "%",
        "target_min": 8.0,
        "target_max": None,
        "warn_min": 4.0,
        "warn_max": None,
        "source_name": "Omega-3 Index Studienlage (Harris et al.)",
        "source_url": "https://www.omega-3index.com",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, Erythrozytenmembran-Messung",
        "recommendations": [
            "Fetten Fisch (Lachs, Makrele, Hering) 2x pro Woche einplanen oder Omega-3-Quelle prüfen.",
            "Verhältnis von Omega-6 zu Omega-3 in der Ernährung im Blick behalten.",
        ],
    },
    "resting_heart_rate": {
        "lower_bound": 50.0,
        "upper_bound": 70.0,
        "penalty_below": 0.0,
        "penalty_above": 0.08,
        "unit": "bpm",
        "target_min": 50.0,
        "target_max": 70.0,
        "warn_min": 40.0,
        "warn_max": 90.0,
        "source_name": "AHA Empfehlungen Ruhepuls",
        "source_url": "https://www.heart.org",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, morgens im Ruhezustand gemessen",
        "recommendations": [
            "Regelmäßiges Ausdauertraining senkt den Ruhepuls bei vielen Menschen nachweislich.",
            "Schlafqualität und Erholung beeinflussen den Ruhepuls spürbar.",
        ],
    },
    "blood_pressure_systolic": {
        "lower_bound": 90.0,
        "upper_bound": 120.0,
        "penalty_below": 0.02,
        "penalty_above": 0.05,
        "unit": "mmHg",
        "target_min": 90.0,
        "target_max": 120.0,
        "warn_min": 80.0,
        "warn_max": 140.0,
        "source_name": "ESC/ESH Blutdruckleitlinie",
        "source_url": "https://www.escardio.org",
        "evidence_level": "hoch",
        "population_note": "Erwachsene, in Ruhe gemessen",
        "recommendations": [
            "Salzarme, kaliumreiche Ernährung kann den Blutdruck unterstützen.",
            "Regelmäßige Bewegung und Stressreduktion sind gut belegte Blutdruck-Hebel.",
        ],
    },
    "blood_pressure_diastolic": {
        "lower_bound": 60.0,
        "upper_bound": 80.0,
        "penalty_below": 0.02,
        "penalty_above": 0.08,
        "unit": "mmHg",
        "target_min": 60.0,
        "target_max": 80.0,
        "warn_min": 50.0,
        "warn_max": 90.0,
        "source_name": "ESC/ESH Blutdruckleitlinie",
        "source_url": "https://www.escardio.org",
        "evidence_level": "hoch",
        "population_note": "Erwachsene, in Ruhe gemessen",
        "recommendations": [
            "Regelmäßige Blutdruckmessung zu ähnlichen Tageszeiten für bessere Vergleichbarkeit.",
            "Alkoholkonsum reduzieren kann den diastolischen Wert positiv beeinflussen.",
        ],
    },
    "waist_circumference": {
        "lower_bound": 0.0,
        "upper_bound": 94.0,
        "penalty_below": 0.0,
        "penalty_above": 0.05,
        "unit": "cm",
        "target_min": 0.0,
        "target_max": 94.0,
        "warn_min": 0.0,
        "warn_max": 102.0,
        "source_name": "IDF Konsensus Taillenumfang",
        "source_url": "https://www.idf.org",
        "evidence_level": "mittel",
        "population_note": "Vereinfachter Richtwert, nicht geschlechtsspezifisch differenziert",
        "recommendations": [
            "Taillenumfang regelmäßig zur gleichen Tageszeit messen für Vergleichbarkeit.",
            "Kombination aus Ernährungsanpassung und Krafttraining wirkt oft am nachhaltigsten.",
        ],
    },
    "sleep_hours": {
        "lower_bound": 7.0,
        "upper_bound": 9.0,
        "penalty_below": 0.3,
        "penalty_above": 0.1,
        "unit": "h",
        "target_min": 7.0,
        "target_max": 9.0,
        "warn_min": 5.0,
        "warn_max": 10.0,
        "source_name": "National Sleep Foundation / AASM",
        "source_url": "https://www.sleepfoundation.org",
        "evidence_level": "hoch",
        "population_note": "Erwachsene, durchschnittliche Schlafdauer pro Nacht",
        "recommendations": [
            "Feste Schlafenszeiten und Bildschirmpause vor dem Schlafengehen einplanen.",
            "Schlafzimmer kühl, dunkel und ruhig halten für bessere Schlafqualität.",
        ],
    },
    "grip_strength": {
        "lower_bound": 35.0,
        "upper_bound": 100.0,
        "penalty_below": 0.03,
        "penalty_above": 0.0,
        "unit": "kg",
        "target_min": 35.0,
        "target_max": None,
        "warn_min": 20.0,
        "warn_max": None,
        "source_name": "Studienlage Griffkraft & Langlebigkeit (Leong et al.)",
        "source_url": "https://www.thelancet.com",
        "evidence_level": "mittel",
        "population_note": "Erwachsene, vereinfachter Richtwert (nicht alters-/geschlechtsspezifisch)",
        "recommendations": [
            "Regelmäßiges Krafttraining, insbesondere Grifftraining, kann die Griffkraft verbessern.",
            "Griffkraft gilt in Studien als Indikator für allgemeine Muskelkraft und Vitalität.",
        ],
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
    fasting_glucose: float = 92.0
    hdl: float = 55.0
    triglycerides: float = 110.0
    homocysteine: float = 9.0
    tsh: float = 1.8
    ferritin: float = 90.0
    vitamin_b12: float = 500.0
    omega3_index: float = 6.0
    resting_heart_rate: float = 65.0
    blood_pressure_systolic: float = 122.0
    blood_pressure_diastolic: float = 78.0
    waist_circumference: float = 88.0
    sleep_hours: float = 6.8
    grip_strength: float = 35.0
    family_context: list[str] = []
    token: str | None = None


# Wellness-only personalization: which existing marker recommendations to prioritize
# based on an optional, self-reported family context. This never changes the
# biological-age calculation itself and adds no new health claims or risk scoring.
FAMILY_CONTEXT_MARKER_FOCUS: dict[str, list[str]] = {
    "herz_kreislauf": [
        "apob",
        "crp",
        "hdl",
        "triglycerides",
        "blood_pressure_systolic",
        "blood_pressure_diastolic",
        "resting_heart_rate",
        "homocysteine",
    ],
    "stoffwechsel": ["hba1c", "fasting_glucose", "triglycerides", "waist_circumference"],
}


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
            db_recommendation = row.get("recommendation")
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
                "recommendations": [db_recommendation] if db_recommendation else DEFAULT_MARKER_CONFIG.get(marker, {}).get("recommendations", ["Marker optimieren."]),
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


def _build_recommendations(
    marker_breakdown: list[dict[str, Any]],
    config: dict[str, dict[str, Any]],
    family_context: list[str] | None = None,
) -> list[str]:
    focus_markers: set[str] = set()
    for context_item in family_context or []:
        focus_markers.update(FAMILY_CONTEXT_MARKER_FOCUS.get(context_item, []))

    sorted_markers = sorted(
        marker_breakdown,
        key=lambda item: (item["marker"] in focus_markers, item["contribution"]),
        reverse=True,
    )
    recommendations: list[str] = []

    for item in sorted_markers:
        if item["contribution"] <= 0:
            continue
        marker = item["marker"]
        tips = config.get(marker, {}).get("recommendations") or ["Marker optimieren."]
        for tip in tips[:2]:
            recommendations.append(str(tip))
        if len(recommendations) >= 6:
            break

    if not recommendations:
        recommendations.append("Marker sind stabil. Werte regelmäßig weiter tracken.")

    return recommendations[:6]


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
        "fasting_glucose": data.fasting_glucose,
        "hdl": data.hdl,
        "triglycerides": data.triglycerides,
        "homocysteine": data.homocysteine,
        "tsh": data.tsh,
        "ferritin": data.ferritin,
        "vitamin_b12": data.vitamin_b12,
        "omega3_index": data.omega3_index,
        "resting_heart_rate": data.resting_heart_rate,
        "blood_pressure_systolic": data.blood_pressure_systolic,
        "blood_pressure_diastolic": data.blood_pressure_diastolic,
        "waist_circumference": data.waist_circumference,
        "sleep_hours": data.sleep_hours,
        "grip_strength": data.grip_strength,
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
    recommendations = _build_recommendations(marker_breakdown, marker_config, data.family_context)

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
        "familienkontext_hinweis": (
            "Deine Empfehlungen wurden auf Basis deines Familienkontexts priorisiert (Wellness-Orientierung, keine Diagnose)."
            if is_premium and data.family_context
            else None
        ),
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