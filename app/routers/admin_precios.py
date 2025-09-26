# app/routers/admin_precios.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Body, HTTPException, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional, Literal, Any, Dict, List
from decimal import Decimal, InvalidOperation

# --- dependencias proyecto ---
try:
    from app.database import get_db
except Exception:
    from app.db import get_db  # ajusta si aplica

try:
    from app.routers.security import require_admin
except Exception:
    try:
        from app.routers.admin_security import require_admin
    except Exception:
        def require_admin():
            return {"username": "admin"}

templates = Jinja2Templates(directory="app/templates")
def render_admin(_templates, request: Request, tpl: str, ctx: dict, _user):
    if "request" not in ctx:
        ctx = {**ctx, "request": request}
    return _templates.TemplateResponse(tpl, ctx)

router = APIRouter(prefix="/admin", tags=["Admin Precios"], dependencies=[Depends(require_admin)])

IVA_TASA_DEFAULT = 19.0
SEGMENTO_PTS = "SUBSCRITO_PTS"

# ---------------- SQL --------------
SQL_COL_EXISTS = text("""
SELECT 1
FROM information_schema.columns
WHERE table_schema = :schema AND table_name = :table AND column_name = :col
LIMIT 1
""")

# Traemos también categoria_id y subcategoria_id para fallback por categoría
SQL_PRODUCTO_BASE = text("""
SELECT p.id_producto,
       p.id_tipo_medicamento,
       p.categoria_id,
       p.subcategoria_id
FROM public.productos p
WHERE p.id_producto = :id
LIMIT 1
""")

SQL_TIPO_MED_BY_ID = text("""
SELECT id_tipo_medicamento, codigo, nombre
FROM public.tipo_medicamento
WHERE id_tipo_medicamento = :id
""")

SQL_LISTA_BY_SLUG = text("""
SELECT id_lista, slug, nombre
FROM public.listas_precios
WHERE slug = :slug
LIMIT 1
""")

SQL_POLITICAS_ACTIVAS = text("""
SELECT id_politica, nombre, id_lista, canal, prioridad, redondeo_estrategia
FROM public.politicas_precio
WHERE activo = TRUE
  AND (canal = :canal OR canal = 'ANY')
ORDER BY prioridad ASC, id_politica ASC
""")

SQL_REGLAS_BY_POL = text("""
SELECT id_regla, id_politica, tipo_formula, markup_pct, descuento_pct, precio_fijo_clp,
       margen_min_pct, tope_pct, considera_iva, rango_costo_min, rango_costo_max,
       prioridad, activo, id_tipo_medicamento
FROM public.reglas_precio
WHERE id_politica = :id_politica AND activo = TRUE
ORDER BY prioridad ASC, id_regla ASC
""")

SQL_PVP_REF_ACTUAL = text("""
SELECT pr.precio_bruto::numeric AS precio_bruto
FROM public.precios pr
JOIN public.listas_precios lp ON lp.id_lista = pr.id_lista
WHERE pr.id_producto = :id_producto
  AND lp.slug = 'pvp'
  AND pr.vigente_hasta IS NULL
ORDER BY pr.vigente_desde DESC
LIMIT 1
""")

SQL_PRECIOS_CERRAR_VIG = text("""
UPDATE public.precios
SET vigente_hasta = now()
WHERE id_producto = :id_producto
  AND id_lista = :id_lista
  AND vigente_hasta IS NULL
""")

SQL_PRECIOS_INSERT = text("""
INSERT INTO public.precios (
  id_producto, id_lista, precio_bruto, iva_tasa, fuente, creado_por
) VALUES (
  :id_producto, :id_lista, :precio_bruto, :iva_tasa, :fuente, :creado_por
)
RETURNING id_precio
""")

SQL_LISTA_BY_ID = text("""
SELECT id_lista, slug, nombre, modo
FROM public.listas_precios
WHERE id_lista = :id
""")

SQL_LISTA_MODO_BY_SLUG = text("SELECT modo FROM public.listas_precios WHERE slug=:slug")

# Parámetros app
SQL_PARAM_GET = text("SELECT valor FROM public.app_parametros WHERE clave=:clave")
SQL_PARAM_SET = text("""
INSERT INTO public.app_parametros (clave, valor)
VALUES (:clave, :valor)
ON CONFLICT (clave) DO UPDATE SET valor=EXCLUDED.valor, actualizado_en=now()
""")

# Márgenes por TIPO
SQL_PTS_MARGEN_GET = text("SELECT margen FROM public.pts_margenes WHERE id_tipo_medicamento=:id")
SQL_PTS_MARGEN_UPSERT = text("""
INSERT INTO public.pts_margenes (id_tipo_medicamento, margen)
VALUES (:id_tipo_medicamento, :margen)
ON CONFLICT (id_tipo_medicamento)
DO UPDATE SET margen=EXCLUDED.margen, actualizado_en=now()
""")
SQL_TIPOS_WITH_MARGEN = text("""
SELECT tm.id_tipo_medicamento, tm.nombre,
       (SELECT margen FROM public.pts_margenes pm
        WHERE pm.id_tipo_medicamento = tm.id_tipo_medicamento) AS margen
FROM public.tipo_medicamento tm
ORDER BY tm.nombre
""")
SQL_TIPO_WITH_MARGEN_BY_ID = text("""
SELECT tm.id_tipo_medicamento,
       tm.nombre,
       (SELECT margen
          FROM public.pts_margenes pm
         WHERE pm.id_tipo_medicamento = tm.id_tipo_medicamento) AS margen
FROM public.tipo_medicamento tm
WHERE tm.id_tipo_medicamento = :id
""")
SQL_TIPOS_SIN_MARGEN = text("""
SELECT tm.id_tipo_medicamento, tm.nombre
FROM public.tipo_medicamento tm
WHERE NOT EXISTS(
  SELECT 1 FROM public.pts_margenes pm
  WHERE pm.id_tipo_medicamento = tm.id_tipo_medicamento
)
ORDER BY tm.nombre
""")

# Márgenes por CATEGORÍA
SQL_PTS_MARGEN_CAT_GET = text("SELECT margen FROM public.pts_margenes_cat WHERE id_categoria=:id")
SQL_PTS_MARGEN_CAT_UPSERT = text("""
INSERT INTO public.pts_margenes_cat (id_categoria, margen)
VALUES (:id_categoria, :margen)
ON CONFLICT (id_categoria)
DO UPDATE SET margen=EXCLUDED.margen, actualizado_en=now()
""")
SQL_CATS_WITH_MARGEN = text("""
SELECT c.id, c.nombre,
       (SELECT margen FROM public.pts_margenes_cat pc
        WHERE pc.id_categoria = c.id) AS margen
FROM public.categorias c
ORDER BY c.nombre
""")
SQL_CAT_WITH_MARGEN_BY_ID = text("""
SELECT c.id, c.nombre,
       (SELECT margen FROM public.pts_margenes_cat pc
        WHERE pc.id_categoria = c.id) AS margen
FROM public.categorias c
WHERE c.id = :id
""")
SQL_CATS_SIN_MARGEN = text("""
SELECT c.id, c.nombre
FROM public.categorias c
WHERE NOT EXISTS(
  SELECT 1 FROM public.pts_margenes_cat pc
  WHERE pc.id_categoria = c.id
)
ORDER BY c.nombre
""")

# ------------- helpers --------------
def _get_param(db: Session, clave: str, default: str | None = None) -> str | None:
    r = db.execute(SQL_PARAM_GET, {"clave": clave}).first()
    return (r[0] if r else default)

def _lista_es_manual_by_slug(db: Session, slug: str) -> bool:
    r = db.execute(SQL_LISTA_MODO_BY_SLUG, {"slug": slug}).first()
    return (r is not None) and (str(r[0]).upper() == "MANUAL")

def _lista_es_manual_by_id(db: Session, id_lista: int) -> bool:
    r = db.execute(SQL_LISTA_BY_ID, {"id": id_lista}).mappings().first()
    return bool(r) and (str(r["modo"]).upper() == "MANUAL")

def _column_exists(db: Session, table: str, col: str, schema: str = "public") -> bool:
    return db.execute(SQL_COL_EXISTS, {"schema": schema, "table": table, "col": col}).first() is not None

def _get_costo_base(db: Session, id_producto: int) -> Optional[float]:
    candidates: List[str] = []
    for c in ("costo_neto", "costo_promedio", "costo_ultimo"):
        if _column_exists(db, "productos", c):
            candidates.append(c)
    if not candidates:
        return None
    cols = ", ".join(candidates)
    row = db.execute(text(f"SELECT {cols} FROM public.productos WHERE id_producto=:id LIMIT 1"),
                     {"id": id_producto}).first()
    if not row:
        return None
    for c in candidates:
        val = row._mapping.get(c)
        if val is not None:
            try:
                return float(val)
            except Exception:
                continue
    return None

def _get_producto_base(db: Session, id_producto: int) -> Dict[str, Any] | None:
    r = db.execute(SQL_PRODUCTO_BASE, {"id": id_producto}).mappings().first()
    return dict(r) if r else None

def _aplicar_redondeo(codigo: str, precio: float) -> int:
    p = float(precio if precio is not None else 0)
    if p <= 0:
        return 0
    if codigo == "PSICO_990":
        miles = int(p // 1000)
        return miles * 1000 + 990
    if codigo == "REDONDEO_100":
        return int(round(p / 100.0) * 100)
    if codigo == "EXACTO":
        return int(round(p))
    return int(round(p))

def _get_pts_margin_for_producto(
    db: Session,
    *,
    id_tipo_medicamento: Optional[int],
    categoria_id: Optional[int],
    subcategoria_id: Optional[int],
) -> Optional[float]:
    # Futuro: subcategoría (si decides crear tabla pts_margenes_subcat)
    # 1) Categoría
    if categoria_id:
        r = db.execute(SQL_PTS_MARGEN_CAT_GET, {"id": int(categoria_id)}).first()
        if r and r[0] is not None:
            try:
                return float(r[0])
            except Exception:
                pass
    # 2) Tipo de medicamento
    if id_tipo_medicamento:
        r = db.execute(SQL_PTS_MARGEN_GET, {"id": int(id_tipo_medicamento)}).first()
        if r and r[0] is not None:
            try:
                return float(r[0])
            except Exception:
                pass
    # 3) Default
    val = _get_param(db, "pts_margen_default", "0.08")
    try:
        return float(val) if val is not None else None
    except Exception:
        return None

# --------- resolver ----------
def resolver_precio(
    db: Session,
    *,
    id_producto: int,
    id_cliente: Optional[int],
    canal: Literal["PTS", "ERP", "ANY"],
) -> Dict[str, Any]:
    pasos: List[str] = []
    info: Dict[str, Any] = {}

    costo = _get_costo_base(db, id_producto)
    base = _get_producto_base(db, id_producto) or {}
    tipo_id = base.get("id_tipo_medicamento")
    categoria_id = base.get("categoria_id")
    subcategoria_id = base.get("subcategoria_id")

    pvp_ref = None
    if canal in ("ANY", "ERP"):
        row = db.execute(SQL_PVP_REF_ACTUAL, {"id_producto": id_producto}).mappings().first()
        pvp_ref = float(row["precio_bruto"]) if row and row["precio_bruto"] is not None else None

    pasos.append(f"costo_base={costo!r}")
    pasos.append(f"id_tipo_medicamento={tipo_id!r} categoria_id={categoria_id!r} subcategoria_id={subcategoria_id!r}")
    pasos.append(f"pvp_ref(vigente)={pvp_ref!r}")

    # 1) Intentar con políticas/reglas (si las usas)
    politicas = db.execute(SQL_POLITICAS_ACTIVAS, {"canal": canal}).mappings().all()
    pasos.append(f"politicas_candidatas={len(politicas)}")

    for pol in politicas:
        pol_id = int(pol["id_politica"])
        pol_nombre = pol["nombre"]
        pol_lista = int(pol["id_lista"])
        pol_redondeo = pol["redondeo_estrategia"] or "EXACTO"

        pasos.append(f"→ política [{pol_id}] '{pol_nombre}' (lista={pol_lista}, redondeo={pol_redondeo})")

        reglas = db.execute(SQL_REGLAS_BY_POL, {"id_politica": pol_id}).mappings().all()
        for rg in reglas:
            # filtros básicos
            rid_tipo = rg.get("id_tipo_medicamento")
            if rid_tipo is not None:
                if (tipo_id is None) or (int(tipo_id) != int(rid_tipo)):
                    pasos.append(f"   - regla {rg['id_regla']}: no matchea tipo_medicamento")
                    continue

            # rango costo
            if costo is not None:
                v = float(costo)
                rmin = rg.get("rango_costo_min")
                rmax = rg.get("rango_costo_max")
                if rmin is not None and v < float(rmin):
                    pasos.append(f"   - regla {rg['id_regla']}: fuera de rango (min)")
                    continue
                if rmax is not None and v > float(rmax):
                    pasos.append(f"   - regla {rg['id_regla']}: fuera de rango (max)")
                    continue

            # aplicar
            tipo_formula = (rg.get("tipo_formula") or "").upper()
            bruto_calc: Optional[float] = None
            if tipo_formula == "COSTO_MAS_MARKUP":
                if costo is not None:
                    mu = float(rg.get("markup_pct") or 0.0) / 100.0
                    bruto_calc = float(costo) * (1.0 + mu)
            elif tipo_formula == "DESCUENTO_SOBRE_PVP":
                if pvp_ref is not None:
                    d = float(rg.get("descuento_pct") or 0.0) / 100.0
                    bruto_calc = float(pvp_ref) * (1.0 - d)
            elif tipo_formula == "PRECIO_FIJO":
                pf = rg.get("precio_fijo_clp")
                bruto_calc = float(pf) if pf is not None else None

            # guardarrailes
            bruto_guard = None
            if bruto_calc is not None:
                pr = float(bruto_calc)
                mm = rg.get("margen_min_pct")
                if mm is not None and costo is not None:
                    min_precio = float(costo) * (1.0 + float(mm) / 100.0)
                    if pr < min_precio:
                        pr = min_precio
                tp = rg.get("tope_pct")
                if tp is not None and pvp_ref is not None:
                    max_precio = float(pvp_ref) * (1.0 + float(tp) / 100.0)
                    if pr > max_precio:
                        pr = max_precio
                bruto_guard = pr

            bruto_final = _aplicar_redondeo(pol_redondeo, bruto_guard if bruto_guard is not None else 0)
            pasos.append(f"   ✓ regla {rg['id_regla']} tipo={tipo_formula} calc={bruto_calc} guard={bruto_guard} redondeo={bruto_final}")
            if bruto_final and bruto_final > 0:
                info = {
                    "ok": True,
                    "precio_bruto": int(bruto_final),
                    "id_lista": pol_lista,
                    "politica": {"id": pol_id, "nombre": pol_nombre},
                    "regla": {"id": int(rg["id_regla"]), "tipo": tipo_formula},
                    "costo_base": costo,
                    "pvp_ref": pvp_ref,
                    "redondeo": pol_redondeo,
                    "pasos": pasos,
                }
                return info

        pasos.append(f"   (política {pol_id}) sin regla aplicable")

    # 2) Fallback PTS por configuración (categoría > tipo > default)
    if canal == "PTS" and costo is not None:
        margen = _get_pts_margin_for_producto(
            db,
            id_tipo_medicamento=tipo_id,
            categoria_id=categoria_id,
            subcategoria_id=subcategoria_id,
        )
        if margen is not None:
            bruto = float(costo) * (1.0 + float(margen))
            redondeo = (_get_param(db, "pts_redondeo", "EXACTO") or "EXACTO").upper()
            bruto_final = _aplicar_redondeo(redondeo, bruto)
            pasos.append(f"fallback_PTS: margen={margen} redondeo={redondeo} calc={bruto} -> {bruto_final}")
            return {
                "ok": True,
                "precio_bruto": int(bruto_final),
                "id_lista": _get_lista_id(db, "pts"),
                "politica": {"id": None, "nombre": "PTS (fallback config)"},
                "regla": {"id": None, "tipo": "COSTO_MAS_MARKUP"},
                "costo_base": costo,
                "pvp_ref": pvp_ref,
                "redondeo": redondeo,
                "pasos": pasos,
            }

    pasos.append("⚠️ Sin regla ni fallback aplicable.")
    return {
        "ok": False,
        "error": "No se encontró regla aplicable o falta costo_base.",
        "costo_base": costo,
        "pvp_ref": pvp_ref,
        "pasos": pasos,
    }

def _publicar_precio(
    db: Session,
    *,
    id_producto: int,
    id_lista: int,
    precio_bruto: int,
    creado_por: str = "admin",
    fuente: str = "admin",
    iva_tasa: float = IVA_TASA_DEFAULT
) -> int:
    db.execute(SQL_PRECIOS_CERRAR_VIG, {"id_producto": id_producto, "id_lista": id_lista})
    rid = db.execute(SQL_PRECIOS_INSERT, {
        "id_producto": id_producto,
        "id_lista": id_lista,
        "precio_bruto": precio_bruto,
        "iva_tasa": iva_tasa,
        "fuente": fuente,
        "creado_por": creado_por,
    }).scalar_one()
    return int(rid)

# ============== UI LISTA ==============
@router.get("/pts/margenes", response_class=HTMLResponse)
def admin_pts_margenes_list_html(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    tipos = db.execute(SQL_TIPOS_WITH_MARGEN).mappings().all()
    categorias = db.execute(SQL_CATS_WITH_MARGEN).mappings().all()
    margen_default = _get_param(db, "pts_margen_default", "0.08") or "0.08"
    ctx = {
        "tipos": tipos,
        "categorias": categorias,
        "margen_default": margen_default,
        "ok": request.query_params.get("ok"),
        "err": request.query_params.get("err"),
    }
    return render_admin(templates, request, "admin_pts_margenes_list.html", ctx, admin_user)

# ============== FORM NUEVO/EDITAR TIPO ==============
@router.get("/pts/margenes/nuevo", response_class=HTMLResponse)
def admin_pts_margen_new_form(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    tipos_sin = db.execute(SQL_TIPOS_SIN_MARGEN).mappings().all()
    if not tipos_sin:
        return RedirectResponse(url="/admin/pts/margenes?ok=all_configured", status_code=303)
    ctx = {
        "item": None,
        "scope": "tipo",
        "tipos": tipos_sin,
        "categorias": [],
        "margen_default": _get_param(db, "pts_margen_default", "0.08") or "0.08",
        "mode": "new",
    }
    return render_admin(templates, request, "admin_pts_margen_form.html", ctx, admin_user)

@router.get("/pts/margenes/{id_tipo_medicamento}/editar", response_class=HTMLResponse)
def admin_pts_margen_edit_form(
    id_tipo_medicamento: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    item = db.execute(SQL_TIPO_WITH_MARGEN_BY_ID, {"id": id_tipo_medicamento}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/pts/margenes?err=tipo_no_encontrado", status_code=303)
    ctx = {
        "item": item,
        "scope": "tipo",
        "tipos": [],
        "categorias": [],
        "margen_default": _get_param(db, "pts_margen_default", "0.08") or "0.08",
        "mode": "edit",
    }
    return render_admin(templates, request, "admin_pts_margen_form.html", ctx, admin_user)

@router.post("/pts/margenes/tipo/guardar")
def admin_pts_margen_tipo_guardar(
    id_tipo_medicamento: int = Form(...),
    margen: str = Form(...),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    try:
        mg = Decimal(margen.replace(",", "."))  # admite coma
        if mg < 0:
            raise ValueError("margen negativo")
        db.execute(SQL_PTS_MARGEN_UPSERT, {
            "id_tipo_medicamento": int(id_tipo_medicamento),
            "margen": mg
        })
        db.commit()
        return RedirectResponse(url="/admin/pts/margenes?ok=saved_tipo", status_code=303)
    except (InvalidOperation, ValueError):
        db.rollback()
        return RedirectResponse(url="/admin/pts/margenes?err=margen_invalido", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/admin/pts/margenes?err=save_error", status_code=303)

# ============== FORM NUEVO/EDITAR CATEGORÍA ==============
@router.get("/pts/margenes/categorias/nuevo", response_class=HTMLResponse)
def admin_pts_margen_cat_new_form(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    cats_sin = db.execute(SQL_CATS_SIN_MARGEN).mappings().all()
    if not cats_sin:
        return RedirectResponse(url="/admin/pts/margenes?ok=all_configured_cat", status_code=303)
    ctx = {
        "item": None,
        "scope": "categoria",
        "tipos": [],
        "categorias": cats_sin,
        "margen_default": _get_param(db, "pts_margen_default", "0.08") or "0.08",
        "mode": "new",
    }
    return render_admin(templates, request, "admin_pts_margen_form.html", ctx, admin_user)

@router.get("/pts/margenes/categorias/{id_categoria}/editar", response_class=HTMLResponse)
def admin_pts_margen_cat_edit_form(
    id_categoria: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    item = db.execute(SQL_CAT_WITH_MARGEN_BY_ID, {"id": id_categoria}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/pts/margenes?err=categoria_no_encontrada", status_code=303)
    ctx = {
        "item": item,
        "scope": "categoria",
        "tipos": [],
        "categorias": [],
        "margen_default": _get_param(db, "pts_margen_default", "0.08") or "0.08",
        "mode": "edit",
    }
    return render_admin(templates, request, "admin_pts_margen_form.html", ctx, admin_user)

@router.post("/pts/margenes/categoria/guardar")
def admin_pts_margen_categoria_guardar(
    id_categoria: int = Form(...),
    margen: str = Form(...),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    try:
        mg = Decimal(margen.replace(",", "."))
        if mg < 0:
            raise ValueError("margen negativo")
        db.execute(SQL_PTS_MARGEN_CAT_UPSERT, {
            "id_categoria": int(id_categoria),
            "margen": mg
        })
        db.commit()
        return RedirectResponse(url="/admin/pts/margenes?ok=saved_categoria", status_code=303)
    except (InvalidOperation, ValueError):
        db.rollback()
        return RedirectResponse(url="/admin/pts/margenes?err=margen_invalido", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/admin/pts/margenes?err=save_error", status_code=303)

# ------------------ API PREVIEW ------------------
@router.get("/api/precios/preview")
def admin_precios_preview(
    request: Request,
    id_producto: int = Query(..., gt=0),
    canal: Literal["PTS", "ERP", "ANY"] = Query("ANY"),
    lista_slug: Optional[str] = Query(None, description="Forzar lista por slug: pvp|pts"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    try:
        if (lista_slug == "pvp") or (canal in ("ANY", "ERP") and _lista_es_manual_by_slug(db, "pvp")):
            row = db.execute(text("""
                SELECT pr.precio_bruto::int AS precio_bruto
                FROM public.precios pr
                JOIN public.listas_precios lp ON lp.id_lista = pr.id_lista
                WHERE pr.id_producto = :idp AND lp.slug='pvp' AND pr.vigente_hasta IS NULL
                ORDER BY pr.vigente_desde DESC
                LIMIT 1
            """), {"idp": id_producto}).mappings().first()

            if row:
                return {
                    "ok": True,
                    "precio_bruto": int(row["precio_bruto"]),
                    "id_lista": _get_lista_id(db, "pvp"),
                    "politica": {"id": None, "nombre": "PVP MANUAL"},
                    "regla": {"id": None, "tipo": "MANUAL"},
                    "pasos": ["PVP es MANUAL → devolvemos precio vigente."]
                }
            else:
                return {"ok": False, "error": "PVP es MANUAL y no hay precio vigente cargado."}

        info = resolver_precio(db, id_producto=id_producto, id_cliente=None, canal=canal)
        return JSONResponse(info)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calculando preview: {e}")

# ------------------ API RECALCULAR ------------------
@router.post("/precios/recalcular")
def admin_precios_recalcular(
    request: Request,
    id_producto: Optional[int] = Body(None),
    id_categoria: Optional[int] = Body(None),
    id_marca: Optional[int] = Body(None),
    aplicar_a: Literal["producto", "categoria", "marca", "todo"] = Body("producto"),
    recalcular_pts: bool = Body(True),
    recalcular_pvp: bool = Body(True),
    force_pvp: bool = Body(False),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    try:
        productos: List[int] = []
        if aplicar_a == "producto":
            if not id_producto:
                raise HTTPException(status_code=400, detail="Falta id_producto para aplicar_a='producto'")
            productos = [int(id_producto)]
        elif aplicar_a == "categoria":
            if not id_categoria:
                raise HTTPException(status_code=400, detail="Falta id_categoria para aplicar_a='categoria'")
            rows = db.execute(text("SELECT id_producto FROM public.productos WHERE categoria_id = :cid"),
                              {"cid": id_categoria}).all()
            productos = [int(r[0]) for r in rows]
        elif aplicar_a == "marca":
            if not id_marca:
                raise HTTPException(status_code=400, detail="Falta id_marca para aplicar_a='marca'")
            rows = db.execute(text("SELECT id_producto FROM public.productos WHERE id_marca = :mid"),
                              {"mid": id_marca}).all()
            productos = [int(r[0]) for r in rows]
        elif aplicar_a == "todo":
            rows = db.execute(text("SELECT id_producto FROM public.productos")).all()
            productos = [int(r[0]) for r in rows]
        else:
            raise HTTPException(status_code=400, detail="aplicar_a inválido")

        if not productos:
            return {"ok": True, "processed": 0, "items": [], "msg": "No hay productos en el alcance."}

        id_lista_pts = _get_lista_id(db, "pts") if recalcular_pts else None
        id_lista_pvp = _get_lista_id(db, "pvp") if recalcular_pvp else None

        pvp_es_manual = False
        if id_lista_pvp:
            r = db.execute(SQL_LISTA_BY_ID, {"id": id_lista_pvp}).mappings().first()
            pvp_es_manual = bool(r) and (str(r["modo"]).upper() == "MANUAL")
        if pvp_es_manual and not force_pvp:
            id_lista_pvp = None

        publicados: List[Dict[str, Any]] = []
        errores: List[Dict[str, Any]] = []

        for pid in productos:
            if id_lista_pts:
                try:
                    info_pts = resolver_precio(db, id_producto=pid, id_cliente=None, canal="PTS")
                    if info_pts.get("ok"):
                        rid = _publicar_precio(
                            db,
                            id_producto=pid,
                            id_lista=id_lista_pts,
                            precio_bruto=int(info_pts["precio_bruto"]),
                            creado_por=str(getattr(admin_user, "username", None) or admin_user.get("username", "admin")),
                            fuente="admin"
                        )
                        publicados.append({"id_producto": pid, "lista": "pts", "id_precio": rid, "precio": info_pts["precio_bruto"]})
                    else:
                        errores.append({"id_producto": pid, "lista": "pts", "error": info_pts.get("error")})
                except Exception as e:
                    errores.append({"id_producto": pid, "lista": "pts", "error": repr(e)})

            if id_lista_pvp:
                try:
                    info_pvp = resolver_precio(db, id_producto=pid, id_cliente=None, canal="ANY")
                    if info_pvp.get("ok"):
                        rid = _publicar_precio(
                            db,
                            id_producto=pid,
                            id_lista=id_lista_pvp,
                            precio_bruto=int(info_pvp["precio_bruto"]),
                            creado_por=str(getattr(admin_user, "username", None) or admin_user.get("username", "admin")),
                            fuente="admin"
                        )
                        publicados.append({"id_producto": pid, "lista": "pvp", "id_precio": rid, "precio": info_pvp["precio_bruto"]})
                    else:
                        errores.append({"id_producto": pid, "lista": "pvp", "error": info_pvp.get("error")})
                except Exception as e:
                    errores.append({"id_producto": pid, "lista": "pvp", "error": repr(e)})

        db.commit()
        return {"ok": True, "processed": len(productos), "publicados": publicados, "errores": errores}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error recalculando precios: {e}")
    