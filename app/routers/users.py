from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str

class LoginRequest(BaseModel):
    email: str
    password: str

@router.post("/register")
async def register(req: RegisterRequest):
    return {"message": "Registrierung erfolgreich (Demo)", "email": req.email}

@router.post("/login")
async def login(req: LoginRequest):
    return {"access_token": "demo-token-123", "message": "Login erfolgreich"}