# app/routers/admin_security.py
from fastapi import APIRouter, Depends, Form, Request, status, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

# Usa SIEMPRE tu utilitario con bcrypt
from app.utils.security_utils import verificar_contrasena
from app.database import get_db

# Reutiliza helpers de seguridad (token, cookie)
from app.routers.security import (
    get_current_user, create_access_token, COOKIE_NAME
)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["Admin"])

SQL_SEL_USER_BY_USUARIO = text("""
    SELECT usuario, contrasena AS password_hash, COALESCE(nombre,'') AS nombre, activo
    FROM usuarios
    WHERE usuario = :usuario
    LIMIT 1
""")

SQL_SEL_ADMIN = text("""
    SELECT usuario, activo
    FROM administradores
    WHERE usuario = :usuario AND activo = TRUE
    LIMIT 1
""")

def _is_admin(db: Session, usuario: str) -> bool:
    row = db.execute(SQL_SEL_ADMIN, {"usuario": usuario}).mappings().first()
    return bool(row and row.get("activo"))

def require_admin(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    usuario = (user or {}).get("usuario")
    if not usuario or not _is_admin(db, usuario):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso administrador requerido"
        )
    return user

@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": admin_user}
    )

@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@router.post("/admin/login")
def admin_login_submit(
    request: Request,
    usuario: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    u = (usuario or "").strip()
    print(f"🟡 [ADMIN LOGIN] Intento de login usuario='{u}'")

    row = db.execute(SQL_SEL_USER_BY_USUARIO, {"usuario": u}).mappings().first()
    print(f"📡 [ADMIN LOGIN] Row devuelto: {row}")

    # Usuario inexistente o inactivo
    if not row or not row.get("activo"):
        print("❌ [ADMIN LOGIN] Usuario no existe o está inactivo")
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Usuario o clave incorrectos"},
            status_code=401
        )

    # Validación bcrypt oficial
    hash_guardado = row.get("password_hash") or ""
    ok = verificar_contrasena(password, hash_guardado)
    print(f"🔑 [ADMIN LOGIN] Verificación contraseña: plain='{password}' hash='{hash_guardado[:30]}...' -> {ok}")

    if not ok:
        print("❌ [ADMIN LOGIN] Contraseña inválida")
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Usuario o clave incorrectos"},
            status_code=401
        )

    # Requiere estar en la tabla administradores
    es_admin = _is_admin(db, row["usuario"])
    print(f"🛡️ [ADMIN LOGIN] ¿Está en tabla administradores? -> {es_admin}")

    if not es_admin:
        print("❌ [ADMIN LOGIN] Usuario no tiene rol de administrador")
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "No tienes permisos de administrador"},
            status_code=403
        )

    # OK -> emitir token y redirigir
    token = create_access_token({"sub": row["usuario"], "role": "admin"})
    print(f"✅ [ADMIN LOGIN] Login exitoso, token emitido para '{row['usuario']}'")

    redirect = RedirectResponse(url="/admin", status_code=303)
    redirect.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,    # ⚠️ en producción: True (HTTPS)
        samesite="lax",
        path="/",
        max_age=60 * 60 * 8,
    )
    return redirect

@router.get("/admin/logout")
def admin_logout():
    r = RedirectResponse(url="/admin/login", status_code=303)
    r.delete_cookie(COOKIE_NAME, path="/")
    return r
