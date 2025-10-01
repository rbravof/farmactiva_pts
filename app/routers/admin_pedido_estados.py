# app/routers/admin_pedido_estados.py
from __future__ import annotations
from typing import Optional, List
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse
from starlette import status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, asc
from sqlalchemy.dialects.postgresql import insert
from pathlib import Path

from app.database import get_db  # típico: app/db.py
from app.routers.admin_security import require_admin
from app.models import PedidoEstado, AppParametro, PedidoTransicion

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

# 🔁 Cambiado el prefijo para evitar colisiones con /admin/pedidos/{id_pedido}
router = APIRouter(prefix="/admin/config/estados-pedido", tags=["Admin · Estados de pedido"])

# Utils simples
def _bool_from_chk(v: Optional[str]) -> bool:
    return v == "on" or v == "true" or v == "1"

# --- NUEVO: construir diagrama Mermaid con estados + transiciones ---
def _build_mermaid(estados: List[PedidoEstado], transiciones: List[PedidoTransicion]) -> str:
    lines = [
        "flowchart LR",
        "classDef st fill:#F3F0FF,stroke:#7C3AED,color:#111,rx:6,ry:6,stroke-width:1.2px;",
    ]

    # Mapear id_estado -> id de nodo mermaid (seguro)
    id2node = {e.id_estado: f"E{e.id_estado}" for e in estados}

    # Nodos (label legible; id seguro)
    for e in estados:
        node_id = id2node[e.id_estado]
        label = f'{e.nombre} ({e.rol_responsable})' if e.rol_responsable else e.nombre
        label = label.replace('"', '\\"')  # evitar romper el string
        lines.append(f'{node_id}["{label}"]')
        lines.append(f"class {node_id} st")

    # Aristas (solo activas; si quieres todas, elimina el if)
    for t in transiciones:
        if getattr(t, "activo", True) is False:
            continue
        a = id2node.get(t.origen_id)
        b = id2node.get(t.destino_id)
        if a and b:
            lines.append(f"{a} --> {b}")

    # 👇 devolver saltos de línea REALES (no '\\n')
    return "\n".join(lines)


# ===========================
# Listado
# ===========================
@router.get("/")
def estados_list(request: Request, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    q = select(PedidoEstado).order_by(asc(PedidoEstado.orden), asc(PedidoEstado.nombre))
    estados = db.execute(q).scalars().all()

    # transiciones activas/inactivas (las mostramos todas; el estilo lo maneja Mermaid)
    trans = db.execute(select(PedidoTransicion)).scalars().all()

    # estado inicial desde app_parametros
    param = db.get(AppParametro, "pedido.estado_inicial")
    inicial: Optional[str] = param.valor if param else None

    # grafo mermaid para el modal
    mermaid_graph = _build_mermaid(estados, trans)

    return templates.TemplateResponse(
        "admin_pedido_estados_list.html",
        {
            "request": request,
            "user": admin_user,
            "estados": estados,
            "estado_inicial_codigo": inicial,
            "mermaid_graph": mermaid_graph,   # <-- pasamos el grafo
        },
    )

# ===========================
# Listado
# ===========================
@router.get("/")
def estados_list(request: Request, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    q = select(PedidoEstado).order_by(asc(PedidoEstado.orden), asc(PedidoEstado.nombre))
    estados = db.execute(q).scalars().all()

    # estado inicial desde app_parametros (clave: pedido.estado_inicial)
    param = db.get(AppParametro, "pedido.estado_inicial")
    inicial: Optional[str] = param.valor if param else None

    return templates.TemplateResponse(
        "admin_pedido_estados_list.html",
        {"request": request, "user": admin_user, "estados": estados, "estado_inicial_codigo": inicial},
    )

# ===========================
# Crear
# ===========================
@router.get("/nuevo")
def estados_new_form(request: Request, admin_user: dict = Depends(require_admin)):
    return templates.TemplateResponse(
        "admin_pedido_estados_form.html",
        {"request": request, "user": admin_user, "mode": "create", "estado": None, "error": None},
    )

@router.post("/guardar")
def estados_create(
    request: Request,
    codigo: str = Form(...),
    nombre: str = Form(...),
    rol_responsable: str = Form(...),
    orden: int = Form(0),
    activo: Optional[str] = Form(None),
    es_final: Optional[str] = Form(None),
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    e = PedidoEstado(
        codigo=codigo.strip(),
        nombre=nombre.strip(),
        rol_responsable=rol_responsable.strip(),
        orden=int(orden or 0),
        activo=_bool_from_chk(activo),
        es_final=_bool_from_chk(es_final),
    )
    try:
        db.add(e)
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            "admin_pedido_estados_form.html",
            {
                "request": request,
                "user": admin_user,
                "mode": "create",
                "estado": e,
                "error": "El código ya existe o los datos no son válidos.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(url="/admin/config/estados-pedido", status_code=status.HTTP_303_SEE_OTHER)

# ===========================
# Editar
# ===========================
@router.get("/{id_estado}/editar")
def estados_edit_form(id_estado: int, request: Request, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    e = db.get(PedidoEstado, id_estado)
    if not e:
        raise HTTPException(status_code=404, detail="Estado no encontrado")
    return templates.TemplateResponse(
        "admin_pedido_estados_form.html",
        {"request": request, "user": admin_user, "mode": "edit", "estado": e, "error": None},
    )

@router.post("/{id_estado}/actualizar")
def estados_update(
    id_estado: int,
    request: Request,
    codigo: str = Form(...),
    nombre: str = Form(...),
    rol_responsable: str = Form(...),
    orden: int = Form(0),
    activo: Optional[str] = Form(None),
    es_final: Optional[str] = Form(None),
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    e = db.get(PedidoEstado, id_estado)
    if not e:
        raise HTTPException(status_code=404, detail="Estado no encontrado")

    e.codigo = codigo.strip()
    e.nombre = nombre.strip()
    e.rol_responsable = rol_responsable.strip()
    e.orden = int(orden or 0)
    e.activo = _bool_from_chk(activo)
    e.es_final = _bool_from_chk(es_final)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            "admin_pedido_estados_form.html",
            {"request": request, "user": admin_user, "mode": "edit", "estado": e, "error": "Código duplicado o datos inválidos."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(url="/admin/config/estados-pedido", status_code=status.HTTP_303_SEE_OTHER)

# ===========================
# Activar/Desactivar
# ===========================
@router.post("/{id_estado}/toggle")
def estados_toggle(id_estado: int, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    e = db.get(PedidoEstado, id_estado)
    if not e:
        raise HTTPException(status_code=404, detail="Estado no encontrado")
    e.activo = not e.activo
    db.commit()
    return RedirectResponse(url="/admin/config/estados-pedido", status_code=status.HTTP_303_SEE_OTHER)

# ===========================
# Parámetros: estado inicial
# ===========================
@router.get("/parametros")
def estados_param_get(request: Request, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    estados = db.execute(select(PedidoEstado).order_by(asc(PedidoEstado.orden), asc(PedidoEstado.nombre))).scalars().all()
    param = db.get(AppParametro, "pedido.estado_inicial")
    inicial_codigo = param.valor if param else None
    return templates.TemplateResponse(
        "admin_pedido_estados_param.html",
        {"request": request, "user": admin_user, "estados": estados, "inicial_codigo": inicial_codigo},
    )

@router.post("/parametros")
def estados_param_post(
    estado_inicial: str = Form(...),
    admin_user: dict = Depends(require_admin),   # o require_staff si quieres permitir QF/AUX
    db: Session = Depends(get_db),
):
    # 1) validar que el estado exista y esté activo
    ok = db.execute(
        select(PedidoEstado.id_estado).where(PedidoEstado.codigo == estado_inicial)
    ).scalar()
    if not ok:
        raise HTTPException(status_code=400, detail="Estado inicial inválido")

    # 2) UPSERT en app_parametros: clave fija "pedido.estado_inicial"
    stmt = (
        insert(AppParametro)
        .values(clave="pedido.estado_inicial", valor=estado_inicial)
        .on_conflict_do_update(
            index_elements=[AppParametro.clave],
            set_={"valor": estado_inicial},
        )
    )
    db.execute(stmt)
    db.commit()

    return RedirectResponse(
        url="/admin/config/estados-pedido",
        status_code=status.HTTP_303_SEE_OTHER
    )

# --- TRANSICIONES: matriz ---
@router.get("/transiciones")
def transiciones_matrix(
    request: Request,
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Estados ordenados
    estados = db.execute(
        select(PedidoEstado).order_by(asc(PedidoEstado.orden), asc(PedidoEstado.nombre))
    ).scalars().all()

    # Transiciones actuales (activas)
    trans_activas = db.execute(select(PedidoTransicion).where(PedidoTransicion.activo == True)).scalars().all()
    checked = {(t.origen_id, t.destino_id) for t in trans_activas}

    return templates.TemplateResponse(
        "admin_pedido_transiciones.html",
        {
            "request": request,
            "user": admin_user,
            "estados": estados,
            "checked": checked,
        },
    )


@router.post("/transiciones")
async def transiciones_save(
    request: Request,
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    form = await request.form()
    # Ej. keys: t_1_2 = on  -> (origen=1, destino=2)
    selected: set[tuple[int, int]] = set()
    for k, v in form.items():
        if not k.startswith("t_"):
            continue
        try:
            _, so, sd = k.split("_", 2)
            o = int(so)
            d = int(sd)
            if o != d and v in ("on", "true", "1"):
                selected.add((o, d))
        except Exception:
            continue

    # Cargar todas las existentes (activas o no)
    existentes = db.execute(select(PedidoTransicion)).scalars().all()
    by_pair: dict[tuple[int, int], PedidoTransicion] = {(t.origen_id, t.destino_id): t for t in existentes}

    # Upsert/soft-delete
    # - Si está seleccionado y no existe -> crear (activo=True)
    # - Si está seleccionado y existe -> activo=True
    # - Si NO está seleccionado y existe activo -> activo=False (soft delete)
    for pair in selected:
        t = by_pair.get(pair)
        if t:
            if not t.activo:
                t.activo = True
        else:
            o, d = pair
            db.add(PedidoTransicion(origen_id=o, destino_id=d, activo=True))

    for pair, t in by_pair.items():
        if pair not in selected and t.activo:
            t.activo = False

    db.commit()
    return RedirectResponse(url="/admin/config/estados-pedido/transiciones", status_code=status.HTTP_303_SEE_OTHER)
