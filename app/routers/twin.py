from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class TwinInput(BaseModel):
    age: int
    gender: str
    hba1c: float = 5.5
    crp: float = 1.2
    vitamin_d: float = 40.0
    apob: float = 80.0


@router.post("/calculate")
async def calculate(data: TwinInput):
    bio_age = data.age * 1.0
    bio_age += (data.hba1c - 5.0) * 2.8
    bio_age += (data.crp - 1.0) * 3.5
    bio_age += max(0, (40 - data.vitamin_d) * 0.6)
    bio_age += max(0, (data.apob - 70) * 0.4)

    return {
        "biologisches_alter": round(bio_age, 1),
        "differenz": round(bio_age - data.age, 1),
        "scenarios": {
            "aktuell": round(bio_age, 1),
            "optimiert": round(bio_age - 5.5, 1),
            "aggressiv": round(bio_age - 9.0, 1),
        },
        "empfehlungen": ["Vitamin D optimieren", "Entzündung reduzieren", "Mehr Bewegung"],
    }
