# app/routers/admin_transportistas.py
from fastapi import APIRouter, Depends, Request, Form, HTTPException, Query
from starlette.responses import RedirectResponse, HTMLResponse
from starlette import status
from sqlalchemy.orm import Session
from sqlalchemy import select, asc, desc, func
from fastapi.templating import Jinja2Templates
from typing import Optional
from app.database import get_db
from app.routers.admin_security import require_admin
from app.models import Transportista, Usuario, UsuarioRol

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/admin/transportistas", tags=["Admin · Transportistas"])

TPL_FORM = "admin_transportista_form.html"  # <- nombre único del template

def _normalize_usuario_ref(val: str | None) -> str | None:
    v = (val or "").strip()
    if not v or v.lower() in ("none", "null", "ninguno", "-", "n/a"):
        return None
    return v

def _bool(v: str | None) -> bool:
    return v in ("on", "true", "1", "True", True)

def _usuarios_transportistas(db: Session):
    return db.execute(
        select(Usuario.usuario, Usuario.nombre)
        .join(UsuarioRol, UsuarioRol.id_usuario == Usuario.id)
        .where(UsuarioRol.rol == "transportista", Usuario.activo == True)
        .order_by(asc(func.lower(Usuario.usuario)))
    ).all()

@router.get("", response_class=HTMLResponse)
def transportistas_list(
    request: Request,
    admin_user: dict = Depends(require_admin),   # Solo administradores ven este módulo
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None),              # búsqueda por nombre/rut/usuario/email/fono
    estado: str = Query("all"),                  # all | activos | inactivos
):
    # Filtros dinámicos
    where_conds = []
    if estado == "activos":
        where_conds.append(Transportista.activo.is_(True))
    elif estado == "inactivos":
        where_conds.append(Transportista.activo.is_(False))

    if q:
        t = f"%{q.strip()}%"
        where_conds.append(
            or_(
                Transportista.nombre.ilike(t),
                Transportista.rut.ilike(t),
                Transportista.usuario.ilike(t),
                Transportista.email.ilike(t),
                Transportista.fono.ilike(t),
            )
        )

    stmt = (
        select(Transportista)
        .where(*where_conds) if where_conds else select(Transportista)
    )
    stmt = stmt.order_by(desc(Transportista.activo), asc(Transportista.nombre))

    rows = db.execute(stmt).scalars().all()
    print(f"[TRANSPORTISTAS] list q={q!r} estado={estado!r} -> {len(rows)} filas")

    return templates.TemplateResponse(
        "admin_transportistas_list.html",
        {
            "request": request,
            "user": admin_user,
            "rows": rows,
            "q": q or "",
            "estado": estado or "all",
        },
    )

@router.get("/nuevo")
def transportistas_new_form(request: Request, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        TPL_FORM,
        {"request": request, "user": admin_user, "item": None, "usuarios_opciones": _usuarios_transportistas(db), "error": None},
    )

@router.post("/guardar")
def transportistas_create(
    request: Request,
    nombre: str = Form(...),
    rut: str = Form(""),
    fono: str = Form(""),
    email: str = Form(""),
    usuario: str = Form(""),
    activo: str = Form("true"),
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    usuario_ref = _normalize_usuario_ref(usuario)

    # Validar FK si viene usuario
    if usuario_ref:
        exists = db.execute(select(Usuario).where(Usuario.usuario == usuario_ref)).scalar_one_or_none()
        if not exists:
            return templates.TemplateResponse(
                TPL_FORM,
                {"request": request, "user": admin_user, "item": None,
                 "usuarios_opciones": _usuarios_transportistas(db),
                 "error": f"El usuario '{usuario_ref}' no existe en el sistema."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    item = Transportista(
        nombre=(nombre or "").strip(),
        rut=(rut or "").strip().upper() or None,
        fono=(fono or "").strip() or None,
        email=(email or "").strip() or None,
        usuario=usuario_ref,
        activo=(str(activo).lower() == "true"),
    )
    db.add(item)
    db.commit()
    return RedirectResponse(url="/admin/transportistas", status_code=status.HTTP_303_SEE_OTHER)

@router.get("/{id_transportista}/editar")
def transportistas_edit_form(
    id_transportista: int,
    request: Request,
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = db.get(Transportista, id_transportista)
    if not item:
        raise HTTPException(status_code=404, detail="Transportista no encontrado")

    return templates.TemplateResponse(
        TPL_FORM,
        {"request": request, "user": admin_user, "item": item,
         "usuarios_opciones": _usuarios_transportistas(db), "error": None},
    )

@router.post("/{id_transportista}/actualizar")
def transportistas_update(
    id_transportista: int,
    request: Request,
    nombre: str = Form(...),
    rut: str = Form(...),
    fono: str | None = Form(None),
    email: str | None = Form(None),
    usuario: str | None = Form(None),
    activo: str | None = Form(None),
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = db.get(Transportista, id_transportista)
    if not item:
        raise HTTPException(status_code=404, detail="Transportista no encontrado")

    usuario_in = _normalize_usuario_ref(usuario)

    # Validaciones del vínculo
    if usuario_in:
        row = db.execute(
            select(Usuario.id)
            .join(UsuarioRol, UsuarioRol.id_usuario == Usuario.id)
            .where(func.lower(Usuario.usuario) == usuario_in.lower(), UsuarioRol.rol == "transportista")
        ).first()
        if not row:
            return templates.TemplateResponse(
                TPL_FORM,
                {"request": request, "user": admin_user, "item": item,
                 "usuarios_opciones": _usuarios_transportistas(db),
                 "error": "El usuario seleccionado no tiene rol Transportista."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # Evitar que el mismo usuario esté vinculado a otro transportista
        clash = db.execute(
            select(Transportista.id_transportista)
            .where(Transportista.usuario == usuario_in, Transportista.id_transportista != id_transportista)
        ).first()
        if clash:
            return templates.TemplateResponse(
                TPL_FORM,
                {"request": request, "user": admin_user, "item": item,
                 "usuarios_opciones": _usuarios_transportistas(db),
                 "error": "Ese usuario ya está vinculado a otro transportista."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    # Persistir
    item.nombre = nombre.strip()
    item.rut = rut.strip()
    item.fono = (fono or "").strip() or None
    item.email = (email or "").strip() or None
    item.usuario = usuario_in
    item.activo = _bool(activo)

    db.commit()
    return RedirectResponse(url="/admin/transportistas", status_code=status.HTTP_303_SEE_OTHER)

@router.post("/{id_transportista}/toggle")
def transportistas_toggle(id_transportista: int, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(Transportista, id_transportista)
    if not item:
        raise HTTPException(404, "Transportista no encontrado")
    item.activo = not item.activo
    db.commit()
    return RedirectResponse(url="/admin/transportistas", status_code=status.HTTP_303_SEE_OTHER)
