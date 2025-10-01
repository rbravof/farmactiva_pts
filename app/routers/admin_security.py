# app/routers/admin_security.py
from fastapi import APIRouter, Depends, Form, Request, status, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text, select
from sqlalchemy.orm import Session

# Verificación de contraseña (bcrypt/compat)
from app.utils.security_utils import verificar_contrasena
from app.database import get_db
from app.models import Usuario, UsuarioRol, Administrador

# Token/cookie helpers ya existentes
from app.routers.security import (
    get_current_user, create_access_token, COOKIE_NAME
)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["Admin"])

# --- Consultas base ---
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

# --- Helpers de rol/permiso ---
ALLOWED_STAFF = {"admin", "qf", "aux"}  # quienes pueden usar el back-office general

def _is_admin(db: Session, usuario: str) -> bool:
    row = db.execute(SQL_SEL_ADMIN, {"usuario": usuario}).mappings().first()
    return bool(row and row.get("activo"))

def _has_role(db, usuario: str, rol: str) -> bool:
    """
    Retorna True si el usuario tiene el rol indicado en usuario_roles.rol.
    Evita relationships del ORM para no chocar con modelos 'roles'.
    """
    row = db.execute(text("""
        SELECT 1
        FROM public.usuarios u
        WHERE u.usuario = :u
          AND EXISTS (
            SELECT 1
            FROM public.usuario_roles ur
            WHERE ur.id_usuario = u.id
              AND ur.rol = :r
          )
        LIMIT 1
    """), {"u": usuario, "r": rol}).first()
    ok = row is not None
    print(f"🛡️ [_has_role] usuario='{usuario}' rol='{rol}' -> {ok}")
    return ok


def _rol_efectivo(db: Session, usuario: str) -> str:
    """Devuelve 'admin' si está en administradores; si no, el rol de usuario_roles; por defecto 'aux'."""
    if _is_admin(db, usuario):
        return "admin"
    row = db.execute(
        select(UsuarioRol.rol).select_from(UsuarioRol).join(Usuario, UsuarioRol.id_usuario == Usuario.id)
        .where(Usuario.usuario == usuario)
        .limit(1)
    ).first()
    return (row[0] if row else "aux") or "aux"

# --- Dependencias de seguridad ---
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
    # <- Enriquecemos el contexto para el template
    return {**(user or {}), "rol": "admin", "is_admin": True}

def require_staff(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    """Permite entrar a admin, qf o aux (backoffice general)."""
    usuario = (user or {}).get("usuario")
    if not usuario:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado")

    rol = _rol_efectivo(db, usuario)
    print(f"🛡️ [STAFF AUTH] usuario='{usuario}' rol='{rol}' -> allowed={rol in ALLOWED_STAFF}")
    if rol not in ALLOWED_STAFF:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso de staff requerido")
    return {**(user or {}), "rol": rol}

def require_transportista(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    usuario = (user or {}).get("usuario")
    if not usuario:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado")

    tiene_rol = _has_role(db, usuario, "transportista")
    print(f"🛡️ [CARRIER AUTH] usuario='{usuario}' rol=transportista -> {tiene_rol}")

    if not tiene_rol:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Rol transportista requerido")
    return user

# --- Dashboard (backoffice landing para admin/qf/aux) ---
@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    admin_user: dict = Depends(require_staff),  # ← antes exigía require_admin
):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": admin_user}
    )

# --- Login ADMIN/QF/AUX (backoffice general) ---
@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    print("[ADMIN LOGIN] GET formulario")
    return templates.TemplateResponse(
        "admin_login.html",
        {
            "request": request,
            "action": "/admin/login",
            "login_title": "Panel de Administración",
            "login_note": "Acceso para administradores, QF y auxiliares.",
        },
    )

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

    if not row or not row.get("activo"):
        print("❌ [ADMIN LOGIN] Usuario no existe o está inactivo")
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "action": "/admin/login",
                "login_title": "Panel de Administración",
                "login_note": "Acceso para administradores, QF y auxiliares.",
                "error": "Usuario o clave incorrectos",
            },
            status_code=401
        )

    hash_guardado = row.get("password_hash") or ""
    ok = verificar_contrasena(password, hash_guardado)
    print(f"🔑 [ADMIN LOGIN] Verificación contraseña -> {ok}")

    if not ok:
        print("❌ [ADMIN LOGIN] Contraseña inválida")
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "action": "/admin/login",
                "login_title": "Panel de Administración",
                "login_note": "Acceso para administradores, QF y auxiliares.",
                "error": "Usuario o clave incorrectos",
            },
            status_code=401
        )

    # Rol efectivo (admin via tabla administradores; si no, usuario_roles)
    rol = _rol_efectivo(db, row["usuario"])
    print(f"🛡️ [ADMIN LOGIN] rol efectivo='{rol}'")

    if rol not in ALLOWED_STAFF:
        print("❌ [ADMIN LOGIN] Usuario no es staff (admin/qf/aux)")
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "action": "/admin/login",
                "login_title": "Panel de Administración",
                "login_note": "Acceso para administradores, QF y auxiliares.",
                "error": "No tienes permisos para el backoffice",
            },
            status_code=403
        )

    # OK -> emitir token y redirigir SIEMPRE al dashboard /admin
    token = create_access_token({"sub": row["usuario"], "role": rol})
    print(f"✅ [ADMIN LOGIN] Login exitoso, token emitido para '{row['usuario']}' con rol='{rol}'")

    redirect = RedirectResponse(url="/admin", status_code=303)
    redirect.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,    # ⚠️ en prod: True (HTTPS)
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

# --- Login TRANSPORTISTA (portal separado) ---
@router.get("/carrier/login", response_class=HTMLResponse)
def carrier_login_form(request: Request):
    return templates.TemplateResponse(
        "admin_login.html",
        {
            "request": request,
            "action": "/carrier/login",
            "login_title": "Portal de Transportistas",
            "login_note": "Acceso exclusivo para transportistas.",
        },
    )

@router.post("/carrier/login")
def carrier_login_submit(
    request: Request,
    usuario: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    u = (usuario or "").strip()
    print(f"🟡 [CARRIER LOGIN] Intento de login usuario='{u}'")

    row = db.execute(SQL_SEL_USER_BY_USUARIO, {"usuario": u}).mappings().first()
    print(f"📡 [CARRIER LOGIN] Row devuelto: {row}")

    if not row or not row.get("activo"):
        print("❌ [CARRIER LOGIN] Usuario no existe o está inactivo")
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "action": "/carrier/login",
                "login_title": "Portal de Transportistas",
                "login_note": "Acceso exclusivo para transportistas.",
                "error": "Usuario o clave incorrectos",
            },
            status_code=401
        )

    hash_guardado = row.get("password_hash") or ""
    ok = verificar_contrasena(password, hash_guardado)
    print(f"🔑 [CARRIER LOGIN] Verificación contraseña -> {ok}")

    if not ok:
        print("❌ [CARRIER LOGIN] Contraseña inválida")
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "action": "/carrier/login",
                "login_title": "Portal de Transportistas",
                "login_note": "Acceso exclusivo para transportistas.",
                "error": "Usuario o clave incorrectos",
            },
            status_code=401
        )

    es_carrier = _has_role(db, row["usuario"], "transportista")
    print(f"🛡️ [CARRIER LOGIN] ¿Tiene rol transportista? -> {es_carrier}")
    if not es_carrier:
        print("❌ [CARRIER LOGIN] Usuario no tiene rol transportista")
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "action": "/carrier/login",
                "login_title": "Portal de Transportistas",
                "login_note": "Acceso exclusivo para transportistas.",
                "error": "No tienes permisos de transportista",
            },
            status_code=403
        )

    token = create_access_token({"sub": row["usuario"], "role": "transportista"})
    print(f"✅ [CARRIER LOGIN] Login exitoso, token emitido para '{row['usuario']}'")

    redirect = RedirectResponse(url="/carrier", status_code=303)
    redirect.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,    # ⚠️ en prod: True (HTTPS)
        samesite="lax",
        path="/",
        max_age=60 * 60 * 8,
    )
    return redirect

@router.get("/carrier/logout")
def carrier_logout():
    r = RedirectResponse(url="/carrier/login", status_code=303)
    r.delete_cookie(COOKIE_NAME, path="/")
    return r
