from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / '.env'
load_dotenv(dotenv_path=ENV_PATH)

app = FastAPI(title="VitalTwin DE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .routers import twin, users, payments

app.include_router(users.router, prefix="/api/users")
app.include_router(twin.router, prefix="/api/twin")
app.include_router(payments.router, prefix="/api/payments")

@app.get("/")
def root():
    return {"message": "VitalTwin Backend läuft"}