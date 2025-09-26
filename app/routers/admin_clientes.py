# app/routers/admin_clientes.py
from fastapi import APIRouter, Depends, Request, Form, Query, Body
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import re

from app.database import get_db
from app.routers.admin_security import require_admin
from app.utils.view import render_admin

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(
    tags=["Admin Clientes"],
    dependencies=[Depends(require_admin)]
)

# ------------------------
# Helpers
# ------------------------
def _bool_from_form(v) -> bool:
    return str(v).strip().lower() in {"1","true","on","sí","si","yes"}

def _normalize_rut(r: str) -> str:
    """
    Normaliza a 'XXXXXXXX-X' (sin puntos). Devuelve '' si vacío/ inválido.
    No valida DV acá; el front ya valida, y el back podría validar luego si quieres.
    """
    if not r:
        return ""
    s = re.sub(r"[^0-9kK]", "", r)
    s = s.upper()
    if len(s) < 2:
        return ""
    body, dv = s[:-1], s[-1]
    return f"{body}-{dv}"

def _bool(v: str | None) -> bool:
    return str(v).lower() in ("1", "true", "on", "yes", "si")

def _to_opt_int(v: str | int | None):
    try:
        if v in ("", None): return None
        return int(v)
    except Exception:
        return None
# ------------------------
# SQLs
# ------------------------
SQL_LIST_BASE = """
SELECT
  c.id_cliente,
  c.nombre,
  c.rut,
  c.email,
  c.telefono,
  c.activo,
  COALESCE(p.cnt, 0) AS pedidos_count
FROM public.clientes c
LEFT JOIN LATERAL (
  SELECT COUNT(*)::int AS cnt
  FROM public.pedidos p
  WHERE p.id_cliente = c.id_cliente
) p ON TRUE
WHERE 1=1
"""

SQL_GET = text("""
SELECT
  id_cliente, nombre, rut, email, telefono,
  notas, acepta_marketing, activo
FROM public.clientes
WHERE id_cliente = :id
""")

SQL_INSERT = text("""
INSERT INTO public.clientes
  (nombre, rut, email, telefono, notas, acepta_marketing, activo)
VALUES
  (:nombre, :rut, :email, :telefono, :notas, :acepta_marketing, :activo)
RETURNING id_cliente
""")

SQL_UPDATE = text("""
UPDATE public.clientes SET
  nombre = :nombre,
  rut = :rut,
  email = :email,
  telefono = :telefono,
  notas = :notas,
  acepta_marketing = :acepta_marketing,
  activo = :activo,
  actualizado_en = now()
WHERE id_cliente = :id_cliente
""")

SQL_EXISTS_RUT = text("SELECT 1 FROM public.clientes WHERE rut = :rut AND (:id IS NULL OR id_cliente <> :id) LIMIT 1")
SQL_EXISTS_EMAIL = text("SELECT 1 FROM public.clientes WHERE lower(email) = lower(:email) AND (:id IS NULL OR id_cliente <> :id) LIMIT 1")

SQL_DIR_LIST = text("""
    SELECT
      d.id_direccion,
      d.id_cliente,
      d.etiqueta,
      d.calle_numero,
      d.depto,
      d.referencia,
      d.id_region,
      r.nombre AS region_nombre,
      d.id_comuna,
      c.nombre AS comuna_nombre,
      d.id_tipo_direccion,
      td.nombre AS tipo_nombre,
      d.es_principal,
      d.activo,
      d.fecha_creacion,
      d.fecha_actualizacion
    FROM public.clientes_direcciones d
    LEFT JOIN public.regiones r ON r.id_region = d.id_region
    LEFT JOIN public.comunas  c ON c.id_comuna  = d.id_comuna
    LEFT JOIN public.tipos_direccion td ON td.id_tipo_direccion = d.id_tipo_direccion
    WHERE d.id_cliente = :id_cliente
    ORDER BY d.es_principal DESC, lower(COALESCE(d.etiqueta,'')) ASC, d.id_direccion ASC
""")

SQL_DIR_INSERT = text("""
    INSERT INTO public.clientes_direcciones
      (id_cliente, etiqueta, calle_numero, depto, referencia,
       id_region, id_comuna, id_tipo_direccion, es_principal, activo)
    VALUES
      (:id_cliente, :etiqueta, :calle_numero, :depto, :referencia,
       :id_region, :id_comuna, :id_tipo_direccion, :es_principal, :activo)
    RETURNING id_direccion
""")

SQL_DIR_UPDATE = text("""
    UPDATE public.clientes_direcciones
    SET
      etiqueta = :etiqueta,
      calle_numero = :calle_numero,
      depto = :depto,
      referencia = :referencia,
      id_region = :id_region,
      id_comuna = :id_comuna,
      id_tipo_direccion = :id_tipo_direccion,
      es_principal = :es_principal,
      activo = :activo,
      fecha_actualizacion = now()
    WHERE id_direccion = :id_direccion
""")

SQL_DIR_GET = text("""
    SELECT
      d.id_direccion, d.id_cliente, d.etiqueta, d.calle_numero, d.depto, d.referencia,
      d.id_region, d.id_comuna, d.id_tipo_direccion, d.es_principal, d.activo
    FROM public.clientes_direcciones d
    WHERE d.id_direccion = :id_direccion
""")

SQL_DIR_CLEAR_PRINCIPAL = text("""
    UPDATE public.clientes_direcciones
    SET es_principal = FALSE
    WHERE id_cliente = :id_cliente
""")

SQL_DIR_CLEAR_PRINCIPAL_POR_TIPO = text("""
    UPDATE public.clientes_direcciones
    SET es_principal = FALSE
    WHERE id_cliente = :id_cliente
      AND (id_tipo_direccion IS NOT DISTINCT FROM :id_tipo_direccion)
""")

SQL_DIR_DELETE_SOFT = text("""
    UPDATE public.clientes_direcciones
    SET activo = FALSE, fecha_actualizacion = now()
    WHERE id_direccion = :id_direccion
""")

SQL_DIR_EXISTS_ETIQUETA = text("""
    SELECT 1
    FROM public.clientes_direcciones
    WHERE id_cliente = :id_cliente
      AND lower(coalesce(etiqueta, '')) = lower(:etiqueta)
    LIMIT 1
""")

SQL_DIR_FIND_BY_LABEL = text("""
    SELECT id_direccion
    FROM public.clientes_direcciones
    WHERE id_cliente = :id_cliente
      AND lower(coalesce(etiqueta, '')) = lower(:etiqueta)
    LIMIT 1
""")
# ------------------------
# Listado
# ------------------------
@router.get("/admin/clientes")
def admin_clientes_list(
    request: Request,
    q: str = None,
    estado: str = None,   # 'activos' | 'inactivos' | None
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    where = []
    params = {}

    if q:
        where.append("(lower(c.nombre) LIKE :q OR lower(c.email) LIKE :q OR c.rut LIKE :q)")
        params["q"] = f"%{q.lower()}%"

    if estado == "activos":
        where.append("c.activo = TRUE")
    elif estado == "inactivos":
        where.append("c.activo = FALSE")

    sql = SQL_LIST_BASE
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY lower(c.nombre) ASC LIMIT 200"

    rows = db.execute(text(sql), params).mappings().all()

    ctx = {
        "rows": rows
    }
    return render_admin(templates, request, "admin_clientes_list.html", ctx, admin_user)

# ------------------------
# Nuevo / Editar (GET)
# ------------------------
@router.get("/admin/clientes/nuevo")
def admin_clientes_new(request: Request, admin_user: dict = Depends(require_admin)):
    return render_admin(templates, request, "admin_cliente_form.html", {"item": None}, admin_user)

@router.get("/admin/clientes/{id_cliente}/editar")
def admin_clientes_edit(
    id_cliente: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    item = db.execute(SQL_GET, {"id": id_cliente}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/clientes", status_code=303)
    return render_admin(templates, request, "admin_cliente_form.html", {"item": item}, admin_user)

# ------------------------
# Crear / Actualizar (POST)
# ------------------------
@router.post("/admin/clientes/nuevo")
def admin_clientes_new_submit(
    request: Request,
    nombre: str = Form(...),
    rut: str = Form(""),
    email: str = Form(""),
    telefono: str = Form(""),
    notas: str = Form(""),
    acepta_marketing: str = Form("false"),
    activo: str = Form("true"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    nombre = (nombre or "").strip()
    rut_norm = _normalize_rut(rut)
    email = (email or "").strip() or None

    if not nombre:
        ctx = {"item": None, "error": "El nombre es obligatorio"}
        return render_admin(templates, request, "admin_cliente_form.html", ctx, admin_user)

    if rut_norm:
        if db.execute(SQL_EXISTS_RUT, {"rut": rut_norm, "id": None}).first():
            ctx = {"item": None, "error": "Ya existe un cliente con ese RUT"}
            return render_admin(templates, request, "admin_cliente_form.html", ctx, admin_user)

    if email:
        if db.execute(SQL_EXISTS_EMAIL, {"email": email, "id": None}).first():
            ctx = {"item": None, "error": "Ya existe un cliente con ese email"}
            return render_admin(templates, request, "admin_cliente_form.html", ctx, admin_user)

    params = {
        "nombre": nombre,
        "rut": rut_norm or None,
        "email": email,
        "telefono": (telefono or "").strip() or None,
        "notas": (notas or "").strip() or None,
        "acepta_marketing": _bool_from_form(acepta_marketing),
        "activo": _bool_from_form(activo),
    }

    print("[CLIENTES nuevo] params:", params)
    new_id = db.execute(SQL_INSERT, params).scalar_one()
    db.commit()
    print("[CLIENTES nuevo] creado id_cliente=", new_id)
    return RedirectResponse(url="/admin/clientes", status_code=303)

@router.post("/admin/clientes/{id_cliente}/editar")
def admin_clientes_edit_submit(
    id_cliente: int,
    request: Request,
    nombre: str = Form(...),
    rut: str = Form(""),
    email: str = Form(""),
    telefono: str = Form(""),
    notas: str = Form(""),
    acepta_marketing: str = Form("false"),
    activo: str = Form("true"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    item = db.execute(SQL_GET, {"id": id_cliente}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/clientes", status_code=303)

    nombre = (nombre or "").strip()
    rut_norm = _normalize_rut(rut)
    email = (email or "").strip() or None

    if not nombre:
        ctx = {"item": item, "error": "El nombre es obligatorio"}
        return render_admin(templates, request, "admin_cliente_form.html", ctx, admin_user)

    if rut_norm:
        if db.execute(SQL_EXISTS_RUT, {"rut": rut_norm, "id": id_cliente}).first():
            ctx = {"item": item, "error": "Ya existe un cliente con ese RUT"}
            return render_admin(templates, request, "admin_cliente_form.html", ctx, admin_user)

    if email:
        if db.execute(SQL_EXISTS_EMAIL, {"email": email, "id": id_cliente}).first():
            ctx = {"item": item, "error": "Ya existe un cliente con ese email"}
            return render_admin(templates, request, "admin_cliente_form.html", ctx, admin_user)

    params = {
        "id_cliente": id_cliente,
        "nombre": nombre,
        "rut": rut_norm or None,
        "email": email,
        "telefono": (telefono or "").strip() or None,
        "notas": (notas or "").strip() or None,
        "acepta_marketing": _bool_from_form(acepta_marketing),
        "activo": _bool_from_form(activo),
    }

    print("[CLIENTES editar] params:", params)
    db.execute(SQL_UPDATE, params)
    db.commit()
    print("[CLIENTES editar] OK id_cliente=", id_cliente)
    return RedirectResponse(url="/admin/clientes", status_code=303)

# ------------------------
# Autocomplete para Pedidos (y el propio form)
# ------------------------
@router.get("/admin/clientes/buscar")
def admin_clientes_buscar(q: str = "", db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    q = (q or "").strip().lower()
    if not q or len(q) < 2:
        return JSONResponse([])

    rows = db.execute(text("""
        SELECT id_cliente AS id, nombre, rut
        FROM public.clientes
        WHERE lower(nombre) LIKE :q OR lower(coalesce(email,'')) LIKE :q OR rut LIKE :q
        ORDER BY lower(nombre)
        LIMIT 20
    """), {"q": f"%{q}%"}).mappings().all()

    return JSONResponse([{"id": r["id"], "nombre": r["nombre"], "rut": r["rut"]} for r in rows])

@router.get("/admin/geo/regiones")
def admin_geo_regiones(db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    rows = db.execute(text("""
        SELECT r.id_region AS id, r.nombre AS nombre
        FROM public.regiones r
        WHERE COALESCE(r.activo, TRUE) = TRUE
        ORDER BY lower(r.nombre)
    """)).mappings().all()

    items = [{"id": r["id"], "nombre": r["nombre"]} for r in rows]
    return {"ok": True, "items": items}   # FastAPI lo serializa

@router.get("/admin/geo/comunas")
def admin_geo_comunas(
    id_region: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    rows = db.execute(text("""
        SELECT c.id_comuna AS id, c.nombre AS nombre
        FROM public.comunas c
        WHERE c.id_region = :id_region AND COALESCE(c.activo, TRUE) = TRUE
        ORDER BY lower(c.nombre)
    """), {"id_region": id_region}).mappings().all()

    items = [{"id": r["id"], "nombre": r["nombre"]} for r in rows]
    return {"ok": True, "items": items}

# ------------------------
# Direcciones Clinte
# ------------------------
@router.get("/admin/clientes/{id_cliente}/direcciones")
def clientes_dir_list(
    id_cliente: int,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    rows = db.execute(SQL_DIR_LIST, {"id_cliente": id_cliente}).mappings().all()
    items = []
    for r in rows:
        items.append({
            "id_direccion": r["id_direccion"],
            "id_cliente": r["id_cliente"],
            "etiqueta": r["etiqueta"],
            "calle_numero": r["calle_numero"],
            "depto": r["depto"],
            "referencia": r["referencia"],
            "id_region": r["id_region"],
            "region": r["region_nombre"],
            "id_comuna": r["id_comuna"],
            "comuna": r["comuna_nombre"],
            "id_tipo_direccion": r["id_tipo_direccion"],
            "tipo": r["tipo_nombre"],
            "es_principal": bool(r["es_principal"]),
            "activo": bool(r["activo"]),
        })
    return {"ok": True, "items": items}

@router.post("/admin/clientes/{id_cliente}/direcciones")
def clientes_dir_create(
    id_cliente: int,
    request: Request,
    etiqueta: str = Form(""),
    calle_numero: str = Form(...),
    depto: str = Form(""),
    referencia: str = Form(""),
    id_region: int = Form(...),
    id_comuna: int = Form(...),
    id_tipo_direccion: str = Form(""),
    es_principal: str = Form("false"),
    activo: str = Form("true"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    etiqueta_clean = (etiqueta or "").strip() or None
    id_tipo_val = _to_opt_int(id_tipo_direccion)
    es_prin = _bool(es_principal)
    activo_b = _bool(activo)

    params_common = {
        "calle_numero": (calle_numero or "").strip(),
        "depto": (depto or "").strip() or None,
        "referencia": (referencia or "").strip() or None,
        "id_region": id_region,
        "id_comuna": id_comuna,
        "id_tipo_direccion": id_tipo_val,
        "es_principal": es_prin,
        "activo": activo_b,
    }

    try:
        # ¿Existe ya una dirección con esta etiqueta?
        existed = None
        if etiqueta_clean:
            existed = db.execute(SQL_DIR_FIND_BY_LABEL, {
                "id_cliente": id_cliente, "etiqueta": etiqueta_clean
            }).first()

        if existed:
            # Actualiza la existente (idempotente)
            id_dir = existed[0]
            if es_prin:
                db.execute(SQL_DIR_CLEAR_PRINCIPAL, {"id_cliente": id_cliente})

            db.execute(SQL_DIR_UPDATE, {
                "id_direccion": id_dir,
                "etiqueta": etiqueta_clean,
                **params_common
            })
            db.commit()
            return {"ok": True, "id_direccion": id_dir, "existed": True, "updated": True}

        # Si no existe, insertamos
        if es_prin:
            db.execute(SQL_DIR_CLEAR_PRINCIPAL, {"id_cliente": id_cliente})

        new_id = db.execute(SQL_DIR_INSERT, {
            "id_cliente": id_cliente,
            "etiqueta": etiqueta_clean,
            **params_common
        }).scalar_one()
        db.commit()
        return {"ok": True, "id_direccion": new_id, "existed": False, "created": True}

    except IntegrityError as e:
        db.rollback()
        # Si por carrera igual chocó el índice, devolvemos un mensaje claro
        if "uq_clientes_direcciones_etiqueta" in str(e.orig):
            return JSONResponse(
                {"ok": False, "error": "Ya existe una dirección con esa etiqueta para este cliente."},
                status_code=400
            )
        return JSONResponse({"ok": False, "error": "No fue posible guardar la dirección."}, status_code=400)

@router.post("/admin/clientes/direcciones/{id_direccion}/editar")
def clientes_dir_edit(
    id_direccion: int,
    etiqueta: str = Form(""),
    calle_numero: str = Form(...),
    depto: str = Form(""),
    referencia: str = Form(""),
    id_region: int = Form(...),
    id_comuna: int = Form(...),
    id_tipo_direccion: str = Form(""),
    es_principal: str = Form("false"),
    activo: str = Form("true"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    cur = db.execute(SQL_DIR_GET, {"id_direccion": id_direccion}).mappings().first()
    if not cur:
        return JSONResponse({"ok": False, "error": "Dirección no encontrada"}, status_code=404)

    id_cliente = cur["id_cliente"]
    etiqueta_clean = (etiqueta or "").strip() or None
    id_tipo_val = _to_opt_int(id_tipo_direccion)
    es_prin = _bool(es_principal)
    activo_b = _bool(activo)

    # Validar duplicado de etiqueta (si viene y cambió)
    if etiqueta_clean and (etiqueta_clean.lower() != (cur["etiqueta"] or "").strip().lower()):
        dup = db.execute(text("""
            SELECT 1
            FROM public.clientes_direcciones
            WHERE id_cliente = :id_cliente
              AND lower(coalesce(etiqueta, '')) = lower(:etiqueta)
              AND id_direccion <> :id_direccion
            LIMIT 1
        """), {
            "id_cliente": id_cliente,
            "etiqueta": etiqueta_clean,
            "id_direccion": id_direccion
        }).first()
        if dup:
            return JSONResponse(
                {"ok": False, "error": "Ya existe otra dirección con esa etiqueta para este cliente."},
                status_code=400
            )

    try:
        if es_prin:
            db.execute(SQL_DIR_CLEAR_PRINCIPAL, {"id_cliente": id_cliente})

        db.execute(SQL_DIR_UPDATE, {
            "id_direccion": id_direccion,
            "etiqueta": etiqueta_clean,
            "calle_numero": calle_numero.strip(),
            "depto": (depto or "").strip() or None,
            "referencia": (referencia or "").strip() or None,
            "id_region": id_region,
            "id_comuna": id_comuna,
            "id_tipo_direccion": id_tipo_val,
            "es_principal": es_prin,
            "activo": activo_b,
        })
        db.commit()
        return {"ok": True}

    except IntegrityError as e:
        db.rollback()
        if "uq_clientes_direcciones_etiqueta" in str(e.orig):
            return JSONResponse(
                {"ok": False, "error": "Etiqueta duplicada para este cliente."},
                status_code=400
            )
        return JSONResponse(
            {"ok": False, "error": "No fue posible actualizar la dirección."},
            status_code=400
        )

@router.post("/admin/clientes/direcciones/{id_direccion}/principal")
def clientes_dir_set_principal(
    id_direccion: int,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    cur = db.execute(SQL_DIR_GET, {"id_direccion": id_direccion}).mappings().first()
    if not cur:
        return JSONResponse({"ok": False, "error": "Dirección no encontrada"}, status_code=404)

    id_cliente = cur["id_cliente"]
    # si usas "principal por tipo", descomenta lo siguiente y comenta el CLEAR global
    # db.execute(SQL_DIR_CLEAR_PRINCIPAL_POR_TIPO, {"id_cliente": id_cliente, "id_tipo_direccion": cur["id_tipo_direccion"]})
    db.execute(SQL_DIR_CLEAR_PRINCIPAL, {"id_cliente": id_cliente})

    db.execute(text("""
        UPDATE public.clientes_direcciones
        SET es_principal = TRUE, fecha_actualizacion = now()
        WHERE id_direccion = :id_direccion
    """), {"id_direccion": id_direccion})
    db.commit()
    return {"ok": True}

@router.post("/admin/clientes/direcciones/{id_direccion}/eliminar")
def clientes_dir_delete(
    id_direccion: int,
    hard: str = Form("false"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    if _bool(hard):
        db.execute(text("DELETE FROM public.clientes_direcciones WHERE id_direccion = :id"), {"id": id_direccion})
    else:
        db.execute(SQL_DIR_DELETE_SOFT, {"id_direccion": id_direccion})
    db.commit()
    return {"ok": True}