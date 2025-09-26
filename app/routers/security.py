# app/routers/security.py
from datetime import datetime, timedelta, timezone
import os
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import text
from sqlalchemy.orm import Session

# ⚠️ IMPORTA get_db desde tu módulo de base de datos real
# (tu main.py ya usa app.database)
from app.database import get_db

# =========================
# Config
# =========================
SECRET_KEY = os.getenv("SECRET_KEY", "cambia_esto_en_.env")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8h

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
templates = Jinja2Templates(directory="app/templates")

COOKIE_NAME = "access_token"
router = APIRouter(tags=["security"])

# =========================
# Helpers
# =========================
def normalize_rut_display(rut: str) -> str:
    # Devuelve algo tipo 12345678-K (solo para mostrar si lo necesitas)
    if not rut:
        return ""
    s = re.sub(r"[^0-9kK\-\.]", "", rut).upper()
    s = s.replace(".", "")
    if "-" not in s and len(s) >= 2:
        s = f"{s[:-1]}-{s[-1]}"
    return s

def compact_rut(rut: str) -> str:
    # Para comparar: solo dígitos + K (sin guion/puntos), upper
    return re.sub(r"[^0-9K]", "", (rut or "").upper())

def verify_password(plain_password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    # Si no parece bcrypt, admite comparación plana (solo dev)
    if not password_hash.startswith("$2"):
        return plain_password == password_hash
    return pwd_context.verify(plain_password, password_hash)

def create_access_token(data: dict, minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES) -> str:
    to_encode = data.copy()
    expire = datetime.now(tz=timezone.utc) + timedelta(minutes=minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def _unauthorized():
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )

def _extract_token(request: Request) -> str | None:
    # Authorization: Bearer xxx
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    # Cookie
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        return tok
    return None

# =========================
# Acceso a usuario sin modelos (SQL crudo)
# =========================
# Ajusta nombres de columnas si difieren en tu BD:
# asumo tabla 'usuarios' con columnas: rut, usuario, contrasena, nombre (opcional)
SQL_SEL_USER_BY_RUT = text("""
    SELECT usuario, rut, contrasena AS password_hash, COALESCE(nombre, '') AS nombre
    FROM usuarios
    WHERE regexp_replace(upper(rut), '[^0-9K]', '', 'g') = :rut_comp
    LIMIT 1
""")

SQL_SEL_USER_BY_USUARIO = text("""
    SELECT usuario, rut, contrasena AS password_hash, COALESCE(nombre, '') AS nombre
    FROM usuarios
    WHERE usuario = :usuario
    LIMIT 1
""")

def fetch_user_by_rut(db: Session, rut_comp: str) -> dict | None:
    row = db.execute(SQL_SEL_USER_BY_RUT, {"rut_comp": rut_comp}).mappings().first()
    return dict(row) if row else None

def fetch_user_by_usuario(db: Session, usuario: str) -> dict | None:
    row = db.execute(SQL_SEL_USER_BY_USUARIO, {"usuario": usuario}).mappings().first()
    return dict(row) if row else None

# =========================
# Dependencia de seguridad
# =========================
def get_current_user(request: Request, db: Session = Depends(get_db)) -> dict:
    token = _extract_token(request)
    if not token:
        raise _unauthorized()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        usuario_login: str | None = payload.get("sub")
        if not usuario_login:
            raise _unauthorized()
    except JWTError:
        raise _unauthorized()

    user = fetch_user_by_usuario(db, usuario_login)
    if not user:
        raise _unauthorized()
    return user  # dict con keys: usuario, rut, password_hash, nombre

# =========================
# Rutas /login /logout
# =========================
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/tienda", error: str | None = None):
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": error})

@router.post("/login")
def login_submit(
    request: Request,
    rut: str = Form(...),
    password: str = Form(...),
    next: str = Form("/tienda"),
    db: Session = Depends(get_db),
):
    rut_comp = compact_rut(rut)
    user = fetch_user_by_rut(db, rut_comp)

    # 🔎 DEBUG opcional (descomenta si necesitas)
    # print(f"[LOGIN] RUT_input={rut!r} rut_comp={rut_comp!r} user_found={bool(user)}")

    if not user or not verify_password(password, user.get("password_hash")):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "RUT o clave incorrectos", "next": next},
            status_code=401,
        )

    token = create_access_token({"sub": user["usuario"]})  # sub SIEMPRE = usuario.login
    redirect = RedirectResponse(url=next or "/tienda", status_code=303)
    redirect.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,   # en prod: True con HTTPS
        samesite="lax",
        path="/",
        max_age=60 * 60 * 8,
    )
    return redirect

@router.get("/logout")
def logout():
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie(COOKIE_NAME, path="/")
    return redirect
