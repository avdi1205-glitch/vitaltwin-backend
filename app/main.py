from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="VitalTwin DE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://vitaltwin.de",
        "https://www.vitaltwin.de",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .routers import twin, users, payments, beta, profile, chat, recommendations, twin_memory

app.include_router(users.router, prefix="/api/users")
app.include_router(twin.router, prefix="/api/twin")
app.include_router(payments.router, prefix="/api/payments")
app.include_router(beta.router, prefix="/api/beta")
app.include_router(profile.router, prefix="/api/profile")
app.include_router(chat.router, prefix="/api/chat")
app.include_router(recommendations.router, prefix="/api/recommendations")
app.include_router(twin_memory.router, prefix="/api/memory")

@app.get("/")
def root():
    return {"message": "VitalTwin Backend läuft"}