# app/routers/admin_envios.py
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.admin_security import require_admin

# --------------------------------
# Routers
# --------------------------------
router = APIRouter()                 # Páginas HTML
api = APIRouter(prefix="/admin/api") # API JSON bajo /admin/api/...

templates = Jinja2Templates(directory="app/templates")

def render_admin(request, template_name, ctx, admin_user):
    data = dict(ctx or {})
    data.update({"request": request, "user": admin_user, "path": request.url.path})
    return templates.TemplateResponse(template_name, data)

# --------------------------------
# SQL
# --------------------------------
SQL_TIPOS_LIST = text("""
  SELECT
    t.id_tipo_envio AS id,
    t.codigo,
    t.nombre,
    t.requiere_direccion,
    t.activo,
    t.orden
  FROM public.tipos_envio t
  ORDER BY t.activo DESC, t.orden ASC, lower(t.nombre) ASC
""")

SQL_TIPO_GET = text("""
  SELECT
    t.id_tipo_envio AS id,
    t.codigo,
    t.nombre,
    t.requiere_direccion,
    t.activo,
    t.orden
  FROM public.tipos_envio t
  WHERE t.id_tipo_envio = :id
""")

SQL_TIPO_INSERT_RETURNING = text("""
  INSERT INTO public.tipos_envio (codigo, nombre, requiere_direccion, activo, orden)
  VALUES (:codigo, :nombre, :requiere_direccion, :activo, :orden)
  RETURNING id_tipo_envio
""")

SQL_TIPO_UPDATE = text("""
  UPDATE public.tipos_envio
  SET codigo = :codigo,
      nombre = :nombre,
      requiere_direccion = :requiere_direccion,
      activo = :activo,
      orden = :orden
  WHERE id_tipo_envio = :id
""")

SQL_TIPO_TOGGLE = text("""UPDATE public.tipos_envio SET activo = NOT activo WHERE id_tipo_envio = :id""")

SQL_TIPOS_OPTIONS = text("""
  SELECT id_tipo_envio AS id, nombre
  FROM public.tipos_envio
  WHERE activo = TRUE
  ORDER BY orden, lower(nombre)
""")

SQL_TARIFAS_LIST = text("""
  SELECT
    t.id_tarifa,
    t.id_tipo_envio,
    te.nombre AS tipo_nombre,
    t.base_clp,
    t.gratis_desde,
    t.peso_min_g,
    t.peso_max_g,
    t.prioridad,
    t.activo,
    r.nombre  AS region_nombre,
    c.nombre  AS comuna_nombre
  FROM public.envio_tarifas t
  JOIN public.tipos_envio te ON te.id_tipo_envio = t.id_tipo_envio
  LEFT JOIN public.regiones r ON r.id_region = t.id_region
  LEFT JOIN public.comunas  c ON c.id_comuna  = t.id_comuna
  ORDER BY te.nombre, t.prioridad ASC, t.base_clp ASC
""")

SQL_TARIFA_GET = text("""
  SELECT
    t.id_tarifa, t.id_tipo_envio, t.id_region, t.id_comuna, t.base_clp,
    t.gratis_desde, t.peso_min_g, t.peso_max_g, t.prioridad, t.activo
  FROM public.envio_tarifas t
  WHERE t.id_tarifa = :id_tarifa
""")

SQL_TARIFA_INSERT_RETURNING = text("""
  INSERT INTO public.envio_tarifas
    (id_tipo_envio, id_region, id_comuna, base_clp, gratis_desde, peso_min_g, peso_max_g, prioridad, activo)
  VALUES
    (:id_tipo_envio, :id_region, :id_comuna, :base_clp, :gratis_desde, :peso_min_g, :peso_max_g, :prioridad, :activo)
  RETURNING id_tarifa
""")

SQL_TARIFA_UPDATE = text("""
  UPDATE public.envio_tarifas
  SET id_tipo_envio=:id_tipo_envio, id_region=:id_region, id_comuna=:id_comuna,
      base_clp=:base_clp, gratis_desde=:gratis_desde, peso_min_g=:peso_min_g,
      peso_max_g=:peso_max_g, prioridad=:prioridad, activo=:activo
  WHERE id_tarifa = :id_tarifa
""")

SQL_TARIFA_DELETE = text("""DELETE FROM public.envio_tarifas WHERE id_tarifa = :id_tarifa""")

SQL_REGIONES_OPTIONS = text("""
  SELECT id_region AS id, nombre
  FROM public.regiones
  WHERE activo IS DISTINCT FROM FALSE
  ORDER BY orden NULLS LAST, lower(nombre)
""")

SQL_COMUNAS_OPTIONS = text("""
  SELECT c.id_comuna AS id, c.id_region, c.nombre
  FROM public.comunas c
  WHERE c.activo IS DISTINCT FROM FALSE
  ORDER BY lower(c.nombre)
""")

# ===========================
# Páginas HTML
# ===========================
@router.get("/admin/envios/tipos", response_class=HTMLResponse)
def envios_tipos_page(request: Request, admin_user: dict = Depends(require_admin)):
    return render_admin(request, "admin_envios_tipos_list.html", {}, admin_user)

@router.get("/admin/envios/tipos/nuevo", response_class=HTMLResponse)
def envios_tipos_new_page(request: Request, admin_user: dict = Depends(require_admin)):
    return render_admin(request, "admin_envios_tipo_form.html", {"item": None}, admin_user)

@router.get("/admin/envios/tipos/{id_tipo}/editar", response_class=HTMLResponse)
def envios_tipos_edit_page(id_tipo: int, request: Request, db: Session = Depends(get_db),
                           admin_user: dict = Depends(require_admin)):
    item = db.execute(SQL_TIPO_GET, {"id": id_tipo}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/envios/tipos", status_code=303)
    return render_admin(request, "admin_envios_tipo_form.html", {"item": item}, admin_user)

@router.get("/admin/envios/tarifas", response_class=HTMLResponse)
def envios_tarifas_page(request: Request, db: Session = Depends(get_db),
                        admin_user: dict = Depends(require_admin)):
    rows = db.execute(SQL_TARIFAS_LIST).mappings().all()
    return render_admin(request, "admin_envios_tarifas_list.html", {"rows": rows}, admin_user)

@router.get("/admin/envios/tarifas/nueva", response_class=HTMLResponse)
def envios_tarifas_new_page(request: Request, db: Session = Depends(get_db),
                            admin_user: dict = Depends(require_admin)):
    tipos    = db.execute(SQL_TIPOS_OPTIONS).mappings().all()
    regiones = db.execute(SQL_REGIONES_OPTIONS).mappings().all()
    comunas  = db.execute(SQL_COMUNAS_OPTIONS).mappings().all()
    ctx = {"item": None, "tipos": tipos, "regiones": regiones, "comunas": comunas}
    return render_admin(request, "admin_envios_tarifa_form.html", ctx, admin_user)

@router.get("/admin/envios/tarifas/{id_tarifa}/editar", response_class=HTMLResponse)
def envios_tarifas_edit_page(id_tarifa: int, request: Request, db: Session = Depends(get_db),
                             admin_user: dict = Depends(require_admin)):
    item = db.execute(SQL_TARIFA_GET, {"id_tarifa": id_tarifa}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/envios/tarifas", status_code=303)
    tipos    = db.execute(SQL_TIPOS_OPTIONS).mappings().all()
    regiones = db.execute(SQL_REGIONES_OPTIONS).mappings().all()
    comunas  = db.execute(SQL_COMUNAS_OPTIONS).mappings().all()
    ctx = {"item": item, "tipos": tipos, "regiones": regiones, "comunas": comunas}
    return render_admin(request, "admin_envios_tarifa_form.html", ctx, admin_user)

# ===========================
# API JSON (prefijo /admin/api)
# ===========================
@api.get("/envios/tipos")
def api_envios_tipos(db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    items = db.execute(SQL_TIPOS_LIST).mappings().all()
    return {"ok": True, "items": [dict(it) for it in items]}

@api.get("/envios/tarifas")
def api_envios_tarifas(db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    items = db.execute(SQL_TARIFAS_LIST).mappings().all()
    return {"ok": True, "items": [dict(it) for it in items]}


# ===========================
# POST (acciones)
# ===========================
@router.post("/admin/envios/tipos/nuevo")
def envios_tipos_new_submit(
    request: Request,
    codigo: str = Form(...),
    nombre: str = Form(...),
    requiere_direccion: str = Form("true"),
    activo: str = Form("true"),
    orden: int = Form(0),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    params = {
        "codigo": (codigo or "").strip(),
        "nombre": (nombre or "").strip(),
        "requiere_direccion": (str(requiere_direccion).lower() == "true"),
        "activo": (str(activo).lower() == "true"),
        "orden": int(orden or 0),
    }
    db.execute(SQL_TIPO_INSERT_RETURNING, params)
    db.commit()
    return RedirectResponse(url="/admin/envios/tipos", status_code=303)

@router.post("/admin/envios/tipos/{id_tipo}/editar")
def envios_tipos_edit_submit(
    id_tipo: int,
    request: Request,
    codigo: str = Form(...),
    nombre: str = Form(...),
    requiere_direccion: str = Form("true"),
    activo: str = Form("true"),
    orden: int = Form(0),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    params = {
        "id": id_tipo,
        "codigo": (codigo or "").strip(),
        "nombre": (nombre or "").strip(),
        "requiere_direccion": (str(requiere_direccion).lower() == "true"),
        "activo": (str(activo).lower() == "true"),
        "orden": int(orden or 0),
    }
    db.execute(SQL_TIPO_UPDATE, params)
    db.commit()
    return RedirectResponse(url="/admin/envios/tipos", status_code=303)

@router.post("/admin/envios/tipos/{id_tipo}/toggle")
def envios_tipos_toggle(id_tipo: int, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    db.execute(SQL_TIPO_TOGGLE, {"id": id_tipo})
    db.commit()
    return RedirectResponse(url="/admin/envios/tipos", status_code=303)

def _to_int_or_none(v: str):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None

@router.post("/admin/envios/tarifas/nueva")
def envios_tarifas_new_submit(
    request: Request,
    id_tipo_envio: int = Form(...),
    id_region: str = Form(""),
    id_comuna: str = Form(""),
    base_clp: str = Form("0"),
    gratis_desde: str = Form(""),
    peso_min_g: str = Form(""),
    peso_max_g: str = Form(""),
    prioridad: str = Form("100"),
    activo: str = Form("true"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    params = {
        "id_tipo_envio": int(id_tipo_envio),
        "id_region": _to_int_or_none(id_region),
        "id_comuna": _to_int_or_none(id_comuna),
        "base_clp": int(base_clp or 0),
        "gratis_desde": _to_int_or_none(gratis_desde),
        "peso_min_g": _to_int_or_none(peso_min_g),
        "peso_max_g": _to_int_or_none(peso_max_g),
        "prioridad": int(prioridad or 100),
        "activo": (str(activo).lower() == "true"),
    }
    db.execute(SQL_TARIFA_INSERT_RETURNING, params)
    db.commit()
    return RedirectResponse(url="/admin/envios/tarifas", status_code=303)

@router.post("/admin/envios/tarifas/{id_tarifa}/editar")
def envios_tarifas_edit_submit(
    id_tarifa: int,
    request: Request,
    id_tipo_envio: int = Form(...),
    id_region: str = Form(""),
    id_comuna: str = Form(""),
    base_clp: str = Form("0"),
    gratis_desde: str = Form(""),
    peso_min_g: str = Form(""),
    peso_max_g: str = Form(""),
    prioridad: str = Form("100"),
    activo: str = Form("true"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    params = {
        "id_tarifa": id_tarifa,
        "id_tipo_envio": int(id_tipo_envio),
        "id_region": _to_int_or_none(id_region),
        "id_comuna": _to_int_or_none(id_comuna),
        "base_clp": int(base_clp or 0),
        "gratis_desde": _to_int_or_none(gratis_desde),
        "peso_min_g": _to_int_or_none(peso_min_g),
        "peso_max_g": _to_int_or_none(peso_max_g),
        "prioridad": int(prioridad or 100),
        "activo": (str(activo).lower() == "true"),
    }
    db.execute(SQL_TARIFA_UPDATE, params)
    db.commit()
    return RedirectResponse(url="/admin/envios/tarifas", status_code=303)

@router.post("/admin/envios/tarifas/{id_tarifa}/eliminar")
def envios_tarifas_delete(id_tarifa: int, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    db.execute(SQL_TARIFA_DELETE, {"id_tarifa": id_tarifa})
    db.commit()
    return RedirectResponse(url="/admin/envios/tarifas", status_code=303)

# <- MUY IMPORTANTE: incluir el sub-router API dentro del router principal
router.include_router(api)
