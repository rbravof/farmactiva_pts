# app/routers/admin_bodegas.py
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.admin_security import require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

def render_admin(request: Request, tpl: str, ctx: dict, user: dict):
    data = dict(ctx or {})
    data.update({"request": request, "user": user})
    return templates.TemplateResponse(tpl, data)

# -------- SQL --------
SQL_LIST = text("""
  SELECT b.id_bodega, b.nombre, b.calle_numero, b.referencia,
         r.nombre AS region, c.nombre AS comuna,
         b.encargado_nombre, b.encargado_email, b.encargado_telefono,
         b.activo, b.orden, b.lat, b.lon
  FROM public.bodegas b
  LEFT JOIN public.regiones r ON r.id_region = b.id_region
  LEFT JOIN public.comunas  c ON c.id_comuna = b.id_comuna
  ORDER BY b.activo DESC, b.orden ASC, lower(b.nombre)
""")

SQL_GET = text("""
  SELECT b.*
  FROM public.bodegas b
  WHERE b.id_bodega = :id
""")

SQL_INSERT = text("""
  INSERT INTO public.bodegas
    (nombre, calle_numero, referencia, id_region, id_comuna, lat, lon,
     encargado_nombre, encargado_email, encargado_telefono, activo, orden)
  VALUES
    (:nombre, :calle_numero, :referencia, :id_region, :id_comuna, :lat, :lon,
     :encargado_nombre, :encargado_email, :encargado_telefono, :activo, :orden)
  RETURNING id_bodega
""")

SQL_UPDATE = text("""
  UPDATE public.bodegas
  SET nombre = :nombre,
      calle_numero = :calle_numero,
      referencia = :referencia,
      id_region = :id_region,
      id_comuna = :id_comuna,
      lat = :lat,
      lon = :lon,
      encargado_nombre = :encargado_nombre,
      encargado_email  = :encargado_email,
      encargado_telefono = :encargado_telefono,
      activo = :activo,
      orden = :orden
  WHERE id_bodega = :id
""")

SQL_TOGGLE = text("UPDATE public.bodegas SET activo = NOT activo WHERE id_bodega = :id")
SQL_DELETE = text("DELETE FROM public.bodegas WHERE id_bodega = :id")

SQL_REGIONES = text("""SELECT id_region AS id, nombre FROM public.regiones ORDER BY orden, lower(nombre)""")
SQL_COMUNAS_BY_REGION = text("""SELECT id_comuna AS id, nombre FROM public.comunas WHERE id_region = :id ORDER BY lower(nombre)""")

# -------- PÁGINAS --------
@router.get("/admin/bodegas", response_class=HTMLResponse)
def bodegas_list(request: Request, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    rows = db.execute(SQL_LIST).mappings().all()
    return render_admin(request, "admin_bodegas_list.html", {"rows": rows}, admin_user)

@router.get("/admin/bodegas/nueva", response_class=HTMLResponse)
def bodegas_new(request: Request, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    regiones = db.execute(SQL_REGIONES).mappings().all()
    return render_admin(request, "admin_bodega_form.html", {"item": None, "regiones": regiones, "comunas": []}, admin_user)

@router.get("/admin/bodegas/{id_bodega}/editar", response_class=HTMLResponse)
def bodegas_edit(id_bodega: int, request: Request, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    item = db.execute(SQL_GET, {"id": id_bodega}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/bodegas", status_code=303)
    regiones = db.execute(SQL_REGIONES).mappings().all()
    comunas = []
    if item["id_region"]:
        comunas = db.execute(SQL_COMUNAS_BY_REGION, {"id": item["id_region"]}).mappings().all()
    return render_admin(request, "admin_bodega_form.html", {"item": item, "regiones": regiones, "comunas": comunas}, admin_user)

# -------- ACCIONES --------
def _to_float_or_none(v: str):
    try:
        v = (v or "").strip()
        return None if v == "" else float(v)
    except Exception:
        return None

def _to_int_or_none(v: str):
    try:
        v = (v or "").strip()
        return None if v == "" else int(v)
    except Exception:
        return None

@router.post("/admin/bodegas/nueva")
def bodegas_new_submit(
    request: Request,
    nombre: str = Form(...),
    calle_numero: str = Form(""),
    referencia: str = Form(""),
    id_region: str = Form(""),
    id_comuna: str = Form(""),
    lat: str = Form(""),
    lon: str = Form(""),
    encargado_nombre: str = Form(""),
    encargado_email: str = Form(""),
    encargado_telefono: str = Form(""),
    activo: str = Form("true"),
    orden: int = Form(0),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    params = {
        "nombre": (nombre or "").strip(),
        "calle_numero": (calle_numero or "").strip(),
        "referencia": (referencia or "").strip(),
        "id_region": _to_int_or_none(id_region),
        "id_comuna": _to_int_or_none(id_comuna),
        "lat": _to_float_or_none(lat),
        "lon": _to_float_or_none(lon),
        "encargado_nombre": (encargado_nombre or "").strip(),
        "encargado_email": (encargado_email or "").strip(),
        "encargado_telefono": (encargado_telefono or "").strip(),
        "activo": (str(activo).lower() == "true"),
        "orden": int(orden or 0),
    }
    db.execute(SQL_INSERT, params)
    db.commit()
    return RedirectResponse(url="/admin/bodegas", status_code=303)

@router.post("/admin/bodegas/{id_bodega}/editar")
def bodegas_edit_submit(
    id_bodega: int,
    request: Request,
    nombre: str = Form(...),
    calle_numero: str = Form(""),
    referencia: str = Form(""),
    id_region: str = Form(""),
    id_comuna: str = Form(""),
    lat: str = Form(""),
    lon: str = Form(""),
    encargado_nombre: str = Form(""),
    encargado_email: str = Form(""),
    encargado_telefono: str = Form(""),
    activo: str = Form("true"),
    orden: int = Form(0),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    params = {
        "id": id_bodega,
        "nombre": (nombre or "").strip(),
        "calle_numero": (calle_numero or "").strip(),
        "referencia": (referencia or "").strip(),
        "id_region": _to_int_or_none(id_region),
        "id_comuna": _to_int_or_none(id_comuna),
        "lat": _to_float_or_none(lat),
        "lon": _to_float_or_none(lon),
        "encargado_nombre": (encargado_nombre or "").strip(),
        "encargado_email": (encargado_email or "").strip(),
        "encargado_telefono": (encargado_telefono or "").strip(),
        "activo": (str(activo).lower() == "true"),
        "orden": int(orden or 0),
    }
    db.execute(SQL_UPDATE, params)
    db.commit()
    return RedirectResponse(url="/admin/bodegas", status_code=303)

@router.post("/admin/bodegas/{id_bodega}/toggle")
def bodegas_toggle(id_bodega: int, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    db.execute(SQL_TOGGLE, {"id": id_bodega})
    db.commit()
    return RedirectResponse(url="/admin/bodegas", status_code=303)

@router.post("/admin/bodegas/{id_bodega}/eliminar")
def bodegas_delete(id_bodega: int, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    db.execute(SQL_DELETE, {"id": id_bodega})
    db.commit()
    return RedirectResponse(url="/admin/bodegas", status_code=303)
