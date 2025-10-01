# app/routers/admin_usuarios.py
from __future__ import annotations
from typing import Optional, List
import secrets, re

from fastapi import APIRouter, Depends, Request, Form, HTTPException, Query
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse, HTMLResponse
from starlette import status
from sqlalchemy import select, asc, and_, func, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from passlib.hash import bcrypt

from app.database import get_db
from app.routers.admin_security import require_admin
from app.models import Usuario, UsuarioRol, Administrador  # Asegúrate de tener Administrador model

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/admin/usuarios", tags=["Admin · Usuarios"])

# ---- NUEVO: roles permitidos (incluye transportista) ----
ALLOWED_ROLES = {"admin", "qf", "aux", "transportista"}

# utils
def _gen_temp_password() -> str:
    base = secrets.token_urlsafe(9)  # ~12 chars
    if not re.search(r"[A-Z]", base): base += "A"
    if not re.search(r"[a-z]", base): base += "a"
    if not re.search(r"\d",   base): base += "3"
    return base

def _hash(pw: str) -> str:
    return bcrypt.hash(pw)

def _rol_efectivo(u: Usuario) -> str:
    # admin desde tabla administradores
    if getattr(u, "admin", None) and getattr(u.admin, "activo", False):
        return "admin"
    # si no es admin, mirar usuario_roles
    if u.rol_ref and u.rol_ref.rol in ALLOWED_ROLES:
        return u.rol_ref.rol
    return "aux"

def _count_admins_activos(db: Session) -> int:
    return db.execute(
        select(Administrador).where(Administrador.activo == True)
    ).scalars().unique().count()

def _normalizar_rut(rut: str) -> str:
    return (rut or "").replace(".", "").replace(" ", "").upper()

def _normalize_username(u: str) -> str:
    u = (u or "").replace("\u00A0", " ").strip()  # NBSP -> espacio, trim
    u = re.sub(r"\s+", ".", u)                    # espacios -> punto (si te gusta)
    return u.lower()

# ===================== Listado =====================
from typing import Optional
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db import get_db
from app.routers.admin_security import require_admin

ALLOWED_ROLES = {"admin", "qf", "aux", "transportista"}

router = APIRouter(prefix="/admin/usuarios", tags=["Usuarios & Roles"])

@router.get("", response_class=HTMLResponse)
def usuarios_list(
    request: Request,
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None),
    rol: Optional[str] = Query(None),        # admin | qf | aux | transportista
    estado: Optional[str] = Query("all"),    # all | activos | inactivos
):
    # Traemos una fila por usuario, con:
    # - is_admin por EXISTS sobre administradores
    # - roles_csv como string_agg de usuario_roles.rol
    stmt = text("""
        SELECT
            u.id,
            u.usuario,
            u.rut,
            u.nombre,
            u.activo,
            EXISTS (
                SELECT 1
                FROM public.administradores a
                WHERE a.usuario = u.usuario
                  AND a.activo IS TRUE
            ) AS is_admin,
            COALESCE(string_agg(ur.rol, ',' ORDER BY ur.rol), '') AS roles_csv
        FROM public.usuarios u
        LEFT JOIN public.usuario_roles ur ON ur.id_usuario = u.id
        GROUP BY u.id, u.usuario, u.rut, u.nombre, u.activo
        ORDER BY u.nombre ASC, u.usuario ASC
    """)
    rows = db.execute(stmt).mappings().all()

    usuarios = []
    term = (q or "").strip().lower()

    # prioridad de rol cuando NO es admin
    prioridad = ["qf", "aux", "transportista"]

    for r in rows:
        # calcular rol efectivo
        if r["is_admin"]:
            rol_calc = "admin"
        else:
            roles = [x for x in (r.get("roles_csv") or "").split(",") if x]
            # elige el primero que aparezca según prioridad
            rol_calc = next((x for x in prioridad if x in roles), "aux")

        # filtros
        if rol and rol_calc != rol:
            continue
        if estado == "activos" and not r["activo"]:
            continue
        if estado == "inactivos" and r["activo"]:
            continue
        if term:
            u = (r["usuario"] or "").lower()
            n = (r["nombre"] or "").lower()
            rt = (r["rut"] or "").lower()
            if term not in u and term not in n and term not in rt:
                continue

        usuarios.append(
            {
                "id": r["id"],
                "usuario": r["usuario"],
                "rut": r["rut"],
                "nombre": r["nombre"],
                "activo": r["activo"],
                "rol": rol_calc,
            }
        )

    return templates.TemplateResponse(
        "admin_usuarios_list.html",
        {
            "request": request,
            "user": admin_user,
            "usuarios": usuarios,
            "q": q or "",
            "rol": rol or "",
            "estado": estado or "all",
        },
    )


# ===================== Crear =====================
@router.get("/nuevo")
def usuarios_new_form(request: Request, admin_user: dict = Depends(require_admin)):
    return templates.TemplateResponse(
        "admin_usuarios_form.html",
        {"request": request, "user": admin_user, "mode": "create", "u": None, "error": None},
    )

@router.post("/guardar")
def usuarios_create(
    request: Request,
    usuario: str = Form(...),
    rut: str = Form(...),
    nombre: str = Form(...),
    rol: str = Form(...),   # admin | qf | aux | transportista
    password: Optional[str] = Form(None),
    temp: Optional[str] = Form(None),  # "on" para generar temporal si password viene vacío
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # ====== TRZ 0: DB y request ======
    try:
        bind = db.get_bind()
        url = getattr(bind, "url", None)
        if url:
            # máscara simple de contraseña en la URL
            url_str = str(url)
            if "@" in url_str and ":" in url_str.split("@")[0]:
                head, tail = url_str.split("@", 1)
                if ":" in head:
                    user_part, _ = head.split(":", 1)
                    url_str = f"{user_part}:***@{tail}"
            print(f"[DB] url={url_str}")
        schema = db.execute(text("SELECT current_schema()")).scalar()
        search_path = db.execute(text("SHOW search_path")).scalar()
        print(f"[DB] current_schema={schema} search_path={search_path}")
    except Exception as e:
        print(f"[DB] introspección falló: {repr(e)}")

    print(f"[USR.CREATE] llamado por admin={admin_user.get('usuario') if isinstance(admin_user, dict) else admin_user}")

    # ====== Normaliza entradas ======
    usuario_in = _normalize_username(usuario)   # NBSP->espacio, trim, lower, etc.
    rut_in = _normalizar_rut(rut)               # quita puntos/espacios, upper
    nombre_in = (nombre or "").strip()

    print(f"[USR.CREATE] RAW usuario={repr(usuario)} rut={repr(rut)} nombre={repr(nombre)} rol={repr(rol)} temp={repr(temp)}")
    print(f"[USR.CREATE] NORM usuario={repr(usuario_in)} rut={repr(rut_in)} nombre={repr(nombre_in)}")

    if rol not in ALLOWED_ROLES:
        print(f"[USR.CREATE] rol inválido: {rol} (permitidos={ALLOWED_ROLES})")
        return templates.TemplateResponse(
            "admin_usuarios_form.html",
            {"request": request, "user": admin_user, "mode": "create",
             "form": {"usuario": usuario_in, "rut": rut_in, "nombre": nombre_in, "rol": rol},
             "error": "Rol inválido."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # password
    if not password and temp == "on":
        password = _gen_temp_password()
        print("[USR.CREATE] password temporal generada (no se muestra)")
    if not password:
        print("[USR.CREATE] sin password y sin temporal -> error")
        return templates.TemplateResponse(
            "admin_usuarios_form.html",
            {"request": request, "user": admin_user, "mode": "create",
             "form": {"usuario": usuario_in, "rut": rut_in, "nombre": nombre_in, "rol": rol},
             "error": "Debe definir una contraseña o marcar 'Generar temporal'."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # ====== Validación: usuario duplicado ======
    try:
        q_user_exact = select(Usuario.id, Usuario.usuario).where(
            func.lower(func.trim(Usuario.usuario)) == usuario_in
        )
        exact_rows = db.execute(q_user_exact).all()
        print(f"[USR.CREATE] check usuario exacto -> rows={len(exact_rows)} {[(r.id, r.usuario) for r in exact_rows]}")

        # Extra: ILIKE para detectar invisibles / variantes
        q_user_like = select(Usuario.id, Usuario.usuario).where(Usuario.usuario.ilike(f"%{usuario_in}%"))
        like_rows = db.execute(q_user_like).all()
        if like_rows:
            print(f"[USR.CREATE] usuario ILIKE -> rows={len(like_rows)} {[(r.id, r.usuario) for r in like_rows]}")

        if exact_rows:
            print(f"[USR.CREATE] DUPLICADO detectado para usuario={repr(usuario_in)}")
            return templates.TemplateResponse(
                "admin_usuarios_form.html",
                {"request": request, "user": admin_user, "mode": "create",
                 "form": {"usuario": usuario_in, "rut": rut_in, "nombre": nombre_in, "rol": rol},
                 "error": "El usuario ya existe."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    except Exception as e:
        print(f"[USR.CREATE] error consultando duplicado usuario: {repr(e)}")

    # ====== Validación: RUT duplicado ======
    try:
        q_rut = select(Usuario.id, Usuario.usuario).where(Usuario.rut == rut_in)
        rut_rows = db.execute(q_rut).all()
        print(f"[USR.CREATE] check RUT exacto -> rows={len(rut_rows)} {[(r.id, r.usuario) for r in rut_rows]}")
        if rut_rows:
            return templates.TemplateResponse(
                "admin_usuarios_form.html",
                {"request": request, "user": admin_user, "mode": "create",
                 "form": {"usuario": usuario_in, "rut": rut_in, "nombre": nombre_in, "rol": rol},
                 "error": "El RUT ya existe."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    except Exception as e:
        print(f"[USR.CREATE] error consultando duplicado RUT: {repr(e)}")

    # ====== Crear usuario ======
    u = Usuario(
        usuario=usuario_in,
        rut=rut_in,
        nombre=nombre_in,
        contrasena=_hash(password),  # guardamos hash
        activo=True,
    )

    try:
        db.add(u)
        db.flush()  # para tener u.id
        print(f"[USR.CREATE] insert provisional id={u.id}")

        _upsert_role_and_admin(db, u, rol)
        print(f"[USR.CREATE] rol aplicado={rol}")

        db.commit()
        print(f"[USR.CREATE] OK usuario={u.usuario} id={u.id} rol={rol}")
    except IntegrityError as ex:
        db.rollback()
        msg = "Usuario o RUT ya existe."
        c_name = None
        try:
            cdiag = getattr(getattr(ex, "orig", None), "diag", None)
            c_name = getattr(cdiag, "constraint_name", None)
        except Exception:
            pass
        print(f"[USR.CREATE] IntegrityError constraint={c_name!r} ex={repr(ex)}")
        if c_name:
            c_low = c_name.lower()
            if "usuario" in c_low: msg = "El usuario ya existe."
            if "rut" in c_low:     msg = "El RUT ya existe."
        return templates.TemplateResponse(
            "admin_usuarios_form.html",
            {"request": request, "user": admin_user, "mode": "create",
             "form": {"usuario": usuario_in, "rut": rut_in, "nombre": nombre_in, "rol": rol},
             "error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as ex:
        db.rollback()
        print(f"[USR.CREATE] EXCEPTION no controlada: {repr(ex)}")
        return templates.TemplateResponse(
            "admin_usuarios_form.html",
            {"request": request, "user": admin_user, "mode": "create",
             "form": {"usuario": usuario_in, "rut": rut_in, "nombre": nombre_in, "rol": rol},
             "error": f"Error al guardar: {str(ex)}"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    print(f"[INFO] Usuario creado: {u.usuario} (pw: {'temporal' if temp=='on' else 'definida'})")
    return RedirectResponse(url="/admin/usuarios", status_code=status.HTTP_303_SEE_OTHER)

# ===================== Editar =====================
@router.get("/{id:int}/editar")
def usuarios_edit_form(id: int, request: Request, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(Usuario, id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    return templates.TemplateResponse(
        "admin_usuarios_form.html",
        {"request": request, "user": admin_user, "mode": "edit", "u": u, "error": None, "rol": _rol_efectivo(u)},
    )

@router.post("/{id:int}/actualizar")
def usuarios_update(
    id: int,
    request: Request,
    rut: str = Form(...),
    nombre: str = Form(...),
    rol: str = Form(...),  # admin | qf | aux | transportista
    activo: Optional[str] = Form(None),
    cambiar_password: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    temp: Optional[str] = Form(None),
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if rol not in ALLOWED_ROLES:
        raise HTTPException(400, "Rol inválido")

    u = db.get(Usuario, id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    u.rut = rut.strip().upper()
    u.nombre = (nombre or "").strip()
    u.activo = (activo == "on")

    # rol/admin
    _upsert_role_and_admin(db, u, rol)

    # password
    if cambiar_password == "on":
        if not password and temp == "on":
            password = _gen_temp_password()
        if password:
            u.contrasena = _hash(password)
            print(f"[INFO] PW cambiada para {u.usuario}: {'temporal' if temp=='on' else 'manual'}")
        else:
            print(f"[WARN] Marcó cambiar password pero no entregó password ni 'temp'")

    db.commit()
    return RedirectResponse(url="/admin/usuarios", status_code=status.HTTP_303_SEE_OTHER)

# ===================== Activar/Desactivar =====================
@router.post("/{id:int}/toggle")
def usuarios_toggle(id: int, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(Usuario, id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    # seguridad: no dejar al sistema sin admin activo
    if _rol_efectivo(u) == "admin" and u.activo and _count_admins_activos(db) <= 1:
        raise HTTPException(400, "Debe existir al menos un Administrador activo.")

    u.activo = not u.activo
    db.commit()
    return RedirectResponse(url="/admin/usuarios", status_code=status.HTTP_303_SEE_OTHER)

# ===================== Reset clave =====================
@router.post("/{id:int}/reset")
def usuarios_reset(id: int, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(Usuario, id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    tmp = _gen_temp_password()
    u.contrasena = _hash(tmp)
    db.commit()
    print(f"[INFO] Reset clave para {u.usuario}. Temporal: {tmp}")
    return RedirectResponse(url="/admin/usuarios", status_code=status.HTTP_303_SEE_OTHER)

# ---------- helpers ----------
def _upsert_role_and_admin(db: Session, u: Usuario, rol: str) -> None:
    """
    - Asigna rol ('admin' | 'qf' | 'aux' | 'transportista') en usuario_roles.
    - Sincroniza tabla 'administradores' según el rol.
    """
    # 1) Upsert del rol en usuario_roles
    ur = db.execute(
        select(UsuarioRol).where(UsuarioRol.id_usuario == u.id)
    ).scalar_one_or_none()
    if ur:
        ur.rol = rol
    else:
        db.add(UsuarioRol(id_usuario=u.id, rol=rol))

    # 2) Sincronizar entrada en 'administradores' por usuario (string)
    adm = db.execute(
        select(Administrador).where(Administrador.usuario == u.usuario)
    ).scalar_one_or_none()

    if rol == "admin":
        if adm:
            adm.activo = True
        else:
            db.add(Administrador(usuario=u.usuario, activo=True))
    else:
        if adm:
            adm.activo = False
