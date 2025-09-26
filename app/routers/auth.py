# app/routers/auth.py
from fastapi import APIRouter, Depends
from app.routers.security import get_current_user  # dependencia ya funcional

router = APIRouter()

@router.get("/api/auth/ping")
def auth_ping():
    return {"ok": True}

@router.get("/api/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    # 'user' viene como dict desde security.get_current_user (usuario, rut, nombre, etc.)
    return {
        "usuario": user.get("usuario"),
        "rut": user.get("rut"),
        "nombre": user.get("nombre", None),
    }
