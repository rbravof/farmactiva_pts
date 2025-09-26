# app/routers/admin_productos.py
from fastapi import APIRouter, Depends, Form, Request, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
import csv, io, re, math, os, unicodedata, time
from typing import List, Optional, Literal
from app.database import get_db
from app.routers.admin_security import require_admin
from app.utils.view import render_admin
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

router = APIRouter(
    tags=["Admin Productos"],
    dependencies=[Depends(require_admin)]
)
templates = Jinja2Templates(directory="app/templates")

# -----------------
# Helpers
# -----------------
def _get_lista_activa_slug(request: Request) -> str:
    slug = request.cookies.get("lista_precio_activa")
    return slug if slug in ("pvp", "pts") else "pts"

def _publicar_precio_vigente(db: Session, id_producto: int, lista_slug: str, precio_bruto: int, creado_por: str = "admin"):
    id_lista = _get_lista_id_by_slug(db, lista_slug)  # ya lo tienes en el router
    db.execute(SQL_PRECIOS_CERRAR_VIG, {"id_producto": id_producto, "id_lista": id_lista})
    rid = db.execute(SQL_PRECIOS_INSERT, {
        "id_producto": id_producto,
        "id_lista": id_lista,
        "precio_bruto": int(precio_bruto),
        "creado_por": creado_por,
    }).scalar_one()
    print(f"✅ [publicado][{lista_slug}] prod={id_producto} id_precio={rid} precio={precio_bruto}")

# Fallback simple para PTS: costo_neto + margen por tipo (si aún no integras el resolver por políticas)
SQL_COSTO_Y_TIPO = text("""
SELECT p.costo_neto, tm.codigo
FROM public.productos p
LEFT JOIN public.tipo_medicamento tm ON tm.id_tipo_medicamento = p.id_tipo_medicamento
WHERE p.id_producto = :id
""")

def _resolver_pts_local(db: Session, id_producto: int) -> Optional[int]:
    row = db.execute(SQL_COSTO_Y_TIPO, {"id": id_producto}).mappings().first()
    if not row or row["costo_neto"] is None:
        return None
    costo = Decimal(row["costo_neto"])
    codigo = (row["codigo"] or "").upper()

    # Márgenes por tipo (ajústalos si quieres)
    if "GEN" in codigo:         m = Decimal("0.20")
    elif "BIO" in codigo:       m = Decimal("0.05")
    elif "MARCA" in codigo:     m = Decimal("0.03")
    else:                       m = Decimal("0.08")  # otros

    bruto = costo * (Decimal("1") + m)
    return int(bruto.to_integral_value(rounding=ROUND_HALF_UP))

def _ascii_slug(text: str) -> str:
    # Transliteración de acentos / símbolos a ASCII
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9\-\.]+", "-", text)       # permite punto para ext
    text = re.sub(r"-{2,}", "-", text)                # colapsa guiones
    return text.strip("-") or "img"

def _safe_filename(original_name: str, ext_whitelist=(".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")) -> tuple[str, str]:
    name, ext = os.path.splitext(original_name.lower())
    if ext not in ext_whitelist:
        ext = ".png"
    safe = _ascii_slug(name)[:40] or "img"
    # sufijo corto para evitar colisiones (segundos + 3 dígitos aleatorios)
    suffix = str(int(time.time()))[-6:]
    return f"{safe}-{suffix}", ext

def _save_images_by_id(files: List[UploadFile], id_producto: int) -> List[str]:
    urls: List[str] = []
    if not files:
        return urls

    fs_base = os.path.join("app", "static", "uploads", "productos", str(id_producto))
    url_base = f"/static/uploads/productos/{id_producto}"
    os.makedirs(fs_base, exist_ok=True)

    print(f"🖼️ [_save_images_by_id] base FS={fs_base}  base URL={url_base}  n={len(files)}")

    for f in files:
        if not f or not getattr(f, "filename", None):
            continue
        stem, ext = _safe_filename(f.filename)
        dest_abs = os.path.join(fs_base, f"{stem}{ext}")
        with open(dest_abs, "wb") as out:
            out.write(f.file.read())
        url = f"{url_base}/{stem}{ext}"
        print(f"   → saved: {dest_abs}  url: {url}")
        urls.append(url)

    return urls

def _save_images(files: List[UploadFile], slug: str) -> List[str]:
    urls: List[str] = []
    if not files:
        return urls

    # Bases de guardado
    fs_base = os.path.join("app", "static", "uploads", "productos", slug or "producto")
    url_base = f"/static/uploads/productos/{slug or 'producto'}"
    os.makedirs(fs_base, exist_ok=True)

    print(f"🖼️ [_save_images] base FS={fs_base}  base URL={url_base}  n={len(files)}")

    for f in files:
        if not f or not getattr(f, "filename", None):
            continue
        name, ext = os.path.splitext(f.filename.lower())
        if ext not in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"]:
            ext = ".png"
        # Normalización básica: solo a-z0-9- y recorte de guiones sueltos
        safe = re.sub(r"[^a-z0-9\-]+", "-", name)[:40].strip("-") or "img"
        dest_abs = os.path.join(fs_base, f"{safe}{ext}")
        with open(dest_abs, "wb") as out:
            out.write(f.file.read())
        url = f"{url_base}/{safe}{ext}"
        print(f"   → saved: {dest_abs}  url: {url}")
        urls.append(url)

    return urls

def _to_decimal(val):
    s = str(val or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None

def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = (s
         .replace("á", "a").replace("é", "e").replace("í", "i")
         .replace("ó", "o").replace("ú", "u").replace("ñ", "n"))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "-"

def _to_float(x):
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    s = str(x).strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        if math.isfinite(v): return v
    except:
        return None
    return None

def _to_int(x):
    if x is None: return None
    try: return int(float(str(x).strip().replace(",", ".")))
    except: return None

def _to_opt_int(s: str):
    s = (s or "").strip()
    try:
        return int(s) if s != "" else None
    except Exception:
        return None

def _bool_from_form(v) -> bool:
    return str(v).lower() in ("true", "1", "on", "yes", "si", "sí", "activo")

def _first_image_for_slug(slug: str) -> Optional[str]:
    """
    Devuelve la primera imagen encontrada para el slug como URL web /static/...,
    buscando en: static/uploads/productos/{slug}/
    """
    if not slug:
        return None

    base_dir = os.path.join("static", "uploads", "productos", slug)
    if not os.path.isdir(base_dir):
        return None

    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
    try:
        files = sorted(
            f for f in os.listdir(base_dir)
            if os.path.isfile(os.path.join(base_dir, f)) and f.lower().endswith(exts)
        )
        if not files:
            return None

        # Construir URL web (ojo con separadores en Windows)
        rel_path = os.path.join("static", "uploads", "productos", slug, files[0]).replace("\\", "/")
        return f"/{rel_path}"
    except Exception:
        return None

def _ensure_pvp_list_id(db: Session) -> int:
    row = db.execute(SQL_PVP_ID).first()
    if row and row[0]:
        return int(row[0])
    new_id = db.execute(SQL_PVP_CREATE).scalar_one()
    db.commit()
    print(f"[precio] Lista PVP creada id_lista={new_id}")
    return int(new_id)

def _get_current_pvp(db: Session, id_producto: int, id_lista: int):
    row = db.execute(SQL_PRECIO_GET_VIGENTE, {"id_producto": id_producto, "id_lista": id_lista}).first()
    return row[0] if row else None

def _set_pvp_price(db: Session, id_producto: int, precio_str: str, admin_user: dict, iva: Decimal = Decimal("19.0")):
    dec = _to_decimal(precio_str)
    if dec is None:
        print("[precio] Sin cambio: precio vacío o inválido.")
        return

    id_lista = _ensure_pvp_list_id(db)
    current = _get_current_pvp(db, id_producto, id_lista)
    if current is not None and Decimal(current) == dec:
        print(f"[precio] Sin cambio: PVP actual ya es {dec}.")
        return

    # cerrar vigente (si hay)
    db.execute(SQL_PRECIO_CERRAR, {"id_producto": id_producto, "id_lista": id_lista})

    creado_por = None
    if isinstance(admin_user, dict):
        creado_por = admin_user.get("email") or admin_user.get("username") or "admin"
    creado_por = creado_por or "admin"

    db.execute(SQL_PRECIO_INSERT, {
        "id_producto": id_producto,
        "id_lista": id_lista,
        "precio_bruto": dec,
        "iva_tasa": iva,
        "fuente": "admin",
        "creado_por": creado_por
    })
    db.commit()
    print(f"[precio] PVP actualizado a {dec} para producto {id_producto} (lista={id_lista})")

# -----------------
# SQL
# -----------------
SQL_LIST = text("""
  SELECT
    p.id_producto,
    p.titulo,
    p.slug,
    p.imagen_principal_url,
    p.visible_web,
    s.nombre AS subcategoria,
    (
      SELECT pr.precio_bruto
      FROM precios pr
      JOIN listas_precios lp ON lp.id_lista = pr.id_lista
      WHERE pr.id_producto = p.id_producto
        AND lp.slug = :slug_activo
        AND pr.vigente_hasta IS NULL
      ORDER BY pr.vigente_desde DESC
      LIMIT 1
    )::numeric AS precio_venta,
    0::int AS stock
  FROM productos p
  LEFT JOIN subcategorias s ON s.id_subcategoria = p.subcategoria_id
  ORDER BY p.visible_web DESC, lower(p.titulo) ASC
  LIMIT 500
""")

SQL_GET = text("""
    SELECT
      p.id_producto            AS id_producto,
      p.id_producto            AS codigo,              -- 👈 alias para "Código interno"
      p.id_producto            AS codigo_interno,      -- 👈 alias alternativo
      p.slug,
      p.titulo                 AS nombre,
      p.descripcion_html       AS descripcion_web,
      p.visible_web,
      p.categoria_id,
      p.subcategoria_id,
      p.id_marca,
      m.nombre                 AS marca_nombre,        -- 👈 nombre legible de la marca
      m.slug                   AS marca_slug,

      p.id_tipo_medicamento,
      p.peso_gramos, p.alto_mm, p.ancho_mm, p.largo_mm,
      p.seo_titulo, p.seo_descripcion,
      p.imagen_principal_url,
      p.costo_neto,
      p.costo_promedio,
      p.costo_ultimo,

      (
        SELECT cb.codigo_barra
        FROM codigos_barras cb
        WHERE cb.id_producto = p.id_producto AND cb.es_principal = TRUE
        ORDER BY cb.id_codigo ASC
        LIMIT 1
      ) AS codigo_barra,                              -- 👈 usado por el form
      (
        SELECT cb.codigo_barra
        FROM codigos_barras cb
        WHERE cb.id_producto = p.id_producto AND cb.es_principal = TRUE
        ORDER BY cb.id_codigo ASC
        LIMIT 1
      ) AS ean,                                       -- 👈 alias alterno por si el template pide "ean"

      (
        SELECT pr.precio_bruto
        FROM precios pr
        JOIN listas_precios lp ON lp.id_lista = pr.id_lista
        WHERE pr.id_producto = p.id_producto
          AND lp.slug = 'pvp'
          AND pr.vigente_hasta IS NULL
        ORDER BY pr.vigente_desde DESC
        LIMIT 1
      )::numeric AS pvp_vigente
    FROM productos p
    LEFT JOIN marcas m ON m.id = p.id_marca           -- 👈 para traer marca_nombre
    WHERE p.id_producto = :id_producto
    LIMIT 1
""")

SQL_INSERT_RETURNING = text("""
  INSERT INTO productos
    (slug, titulo, descripcion_html, seo_titulo, seo_descripcion,
     imagen_principal_url,
     visible_web, requiere_receta,
     peso_gramos, ancho_mm, alto_mm, largo_mm,
     categoria_id, subcategoria_id, id_marca, id_tipo_medicamento,
     costo_neto, costo_promedio, costo_ultimo)                       -- <<< NUEVO
  VALUES
    (:slug, :nombre, :descripcion_web, :seo_titulo, :seo_descripcion,
     :imagen_principal_url,
     :visible_web, :requiere_receta,
     :peso_gramos, :ancho_mm, :alto_mm, :largo_mm,
     :categoria_id, :subcategoria_id, :id_marca, :id_tipo_medicamento,
     :costo_neto, :costo_promedio, :costo_ultimo)                    -- <<< NUEVO
  RETURNING id_producto
""")

SQL_UPDATE = text("""
  UPDATE public.productos SET
    slug = :slug,
    titulo = :nombre,
    descripcion_html = :descripcion_web,
    seo_titulo = :seo_titulo,
    seo_descripcion = :seo_descripcion,
    imagen_principal_url = :imagen_principal_url,
    visible_web = :visible_web,
    requiere_receta = :requiere_receta,
    peso_gramos = :peso_gramos,
    ancho_mm = :ancho_mm,
    alto_mm = :alto_mm,
    largo_mm = :largo_mm,
    categoria_id = :categoria_id,
    subcategoria_id = :subcategoria_id,
    id_marca = :id_marca,
    id_tipo_medicamento = :id_tipo_medicamento,
    costo_neto = :costo_neto,
    costo_promedio = :costo_promedio,
    costo_ultimo = :costo_ultimo,
    fecha_actualizacion = now()          -- 👈 corregido
  WHERE id_producto = :id_producto
""")

SQL_EXISTS_SLUG = text("""
  SELECT 1 FROM productos WHERE slug = :slug LIMIT 1
""")

SQL_DELETE_ID = text("DELETE FROM productos WHERE id_producto = :id_producto")

SQL_CATEGORIAS_OPCIONES = text("""
  SELECT id, nombre
  FROM categorias
  ORDER BY visible DESC, orden ASC, lower(nombre) ASC
""")

# Listar subcategorías por categoría
SQL_SUBCATS_BY_CAT = text("""
SELECT id_subcategoria, nombre
FROM subcategorias
WHERE id_categoria = :id_categoria AND activo = TRUE
ORDER BY LOWER(nombre) ASC
""")

# Buscar subcategoría por nombre (para alta on-the-fly)
SQL_SUBCAT_FIND_BY_NAME = text("""
SELECT id_subcategoria
FROM subcategorias
WHERE id_categoria = :id_categoria AND LOWER(nombre) = LOWER(:nombre)
LIMIT 1
""")

# Insertar subcategoría
SQL_SUBCAT_INSERT = text("""
INSERT INTO subcategorias (id_categoria, nombre, slug, activo)
VALUES (:id_categoria, :nombre, :slug, TRUE)
RETURNING id_subcategoria
""")

# Marcas (autocomplete + alta)
SQL_MARCA_FIND_BY_NAME = text("""
  SELECT id FROM marcas WHERE lower(nombre) = lower(:nombre) LIMIT 1
""")

SQL_MARCA_INSERT = text("""
  INSERT INTO marcas (nombre, slug, visible, orden)
  VALUES (:nombre, :slug, TRUE, 0)
  RETURNING id
""")

SQL_MARCAS_SEARCH = text("""
    SELECT id, nombre, slug, logo_url
    FROM marcas
    WHERE lower(nombre) LIKE lower(:q)
    ORDER BY visible DESC, orden ASC, lower(nombre) ASC
    LIMIT 10
""")

# Códigos de barra
SQL_CB_INSERT = text("""
  INSERT INTO codigos_barras (id_producto, codigo_barra, es_principal)
  VALUES (:id_producto, :codigo_barra, TRUE)
  ON CONFLICT (codigo_barra) DO NOTHING
""")

SQL_CB_CLEAR_PRINCIPAL = text("""
  UPDATE codigos_barras
  SET es_principal = FALSE
  WHERE id_producto = :id_producto AND es_principal = TRUE
""")

SQL_PVP_ID = text("""
SELECT id_lista
FROM listas_precios
WHERE slug = 'pvp'
LIMIT 1
""")

SQL_PVP_CREATE = text("""
INSERT INTO listas_precios (nombre, slug, descripcion, prioridad, activo)
VALUES ('PVP', 'pvp', 'Precio público general', 10, TRUE)
RETURNING id_lista
""")

SQL_PRECIO_GET_VIGENTE = text("""
SELECT precio_bruto
FROM precios
WHERE id_producto = :id_producto
  AND id_lista = :id_lista
  AND vigente_hasta IS NULL
LIMIT 1
""")

SQL_PRECIO_CERRAR = text("""
UPDATE precios
SET vigente_hasta = NOW()
WHERE id_producto = :id_producto
  AND id_lista = :id_lista
  AND vigente_hasta IS NULL
""")

SQL_PRECIO_INSERT = text("""
INSERT INTO precios (id_producto, id_lista, precio_bruto, iva_tasa, fuente, creado_por)
VALUES (:id_producto, :id_lista, :precio_bruto, :iva_tasa, :fuente, :creado_por)
""")

SQL_TIPOS_MED = text("""
SELECT id_tipo_medicamento, codigo, nombre
FROM public.tipo_medicamento
WHERE activo = TRUE
ORDER BY nombre
""")

SQL_TIPO_MED_EXISTS = text("""
SELECT 1
FROM public.tipo_medicamento
WHERE id_tipo_medicamento = :id AND activo = TRUE
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
INSERT INTO public.precios (id_producto, id_lista, precio_bruto, iva_tasa, fuente, creado_por)
VALUES (:id_producto, :id_lista, :precio_bruto, 19.0, 'admin', :creado_por)
RETURNING id_precio
""")

def _get_lista_id_by_slug(db: Session, slug: str) -> int:
    r = db.execute(text("SELECT id_lista FROM public.listas_precios WHERE slug=:slug"), {"slug": slug}).first()
    if not r: raise HTTPException(status_code=400, detail=f"Lista no encontrada: {slug}")
    return int(r[0])

def _publicar_pvp_manual(db: Session, id_producto: int, pvp_bruto: int, creado_por: str = "admin"):
    id_lista = _get_lista_id_by_slug(db, "pvp")
    db.execute(SQL_PRECIOS_CERRAR_VIG, {"id_producto": id_producto, "id_lista": id_lista})
    rid = db.execute(SQL_PRECIOS_INSERT, {
        "id_producto": id_producto,
        "id_lista": id_lista,
        "precio_bruto": int(pvp_bruto),
        "creado_por": creado_por,
    }).scalar_one()
    print(f"✅ [PVP MANUAL] prod={id_producto} id_precio={rid} precio={pvp_bruto}")

# -----------------
# LISTA
# -----------------
@router.get("/admin/productos", response_class=HTMLResponse)
def admin_productos_list(request: Request, admin_user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    slug_activo = _get_lista_activa_slug(request)
    rows = db.execute(SQL_LIST, {"slug_activo": slug_activo}).mappings().all()
    return templates.TemplateResponse(
        "admin_producto_list.html",
        {"request": request, "rows": rows, "user": admin_user, "slug_activo": slug_activo}
    )

# -----------------
# BUSCAR MARCAS (JSON)
# -----------------
@router.get("/admin/marcas/buscar")
def admin_marcas_buscar(q: str = "", db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    q = (q or "").strip()
    if len(q) < 3:
        return JSONResponse([])
    rows = db.execute(SQL_MARCAS_SEARCH, {"q": f"%{q}%"}).mappings().all()
    return JSONResponse([{"id": r["id"], "nombre": r["nombre"], "slug": r["slug"], "logo_url": r["logo_url"]} for r in rows])

# -----------------
# NUEVO
# -----------------
@router.get("/admin/productos/nuevo", response_class=HTMLResponse)
def admin_productos_new_form(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    categorias = db.execute(SQL_CATEGORIAS_OPCIONES).mappings().all()
    tipos = db.execute(SQL_TIPOS_MED).mappings().all()

    # lista de precios activa por sesión (cookie), fallback a 'pts'
    lista_activa = request.cookies.get("lista_precio_activa")
    if lista_activa not in ("pvp", "pts"):
        lista_activa = "pts"

    ctx = {
        "item": None,
        "categorias": categorias,
        "tipos_medicamento": tipos,
        "lista_activa": lista_activa,
    }
    return render_admin(templates, request, "admin_producto_form.html", ctx, admin_user)

@router.post("/admin/productos/nuevo")
def admin_productos_new_submit(
    request: Request,
    nombre: str = Form(...),
    laboratorio: str = Form(""),
    slug: str = Form(""),
    descripcion_web: str = Form(""),
    seo_titulo: str = Form(""),
    seo_descripcion: str = Form(""),
    visible_web: str = Form("false"),
    tipo_receta: str = Form("Venta Libre"),

    categoria_id_raw: str = Form(""),
    subcategoria_id_raw: str = Form(""),
    marca_id_raw: str = Form(""),
    marca_nombre: str = Form(""),

    id_tipo_medicamento: int = Form(...),

    codigo_barra: str = Form(""),

    # >>>>>> PVP manual y costos <<<<<<
    pvp_bruto_manual: str = Form(""),
    stock: str = Form(""),

    peso_gramos: str = Form(""),
    alto_mm: str = Form(""),
    ancho_mm: str = Form(""),
    largo_mm: str = Form(""),

    costo_neto: str = Form(""),
    costo_promedio: str = Form(""),
    costo_ultimo: str = Form(""),

    imagenes: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    print("[POST nuevo] nombre=", nombre, " visible_web=", visible_web, " tipo_receta=", tipo_receta)

    # --- Normalización de inputs
    nombre = (nombre or "").strip()
    slug = (slug or "").strip() or _slugify(nombre)
    visible_val = _bool_from_form(visible_web)
    requiere_receta = (str(tipo_receta).strip().lower() != "venta libre")
    categoria_id = _to_opt_int(categoria_id_raw)
    subcategoria_id = _to_opt_int(subcategoria_id_raw)

    categorias = db.execute(SQL_CATEGORIAS_OPCIONES).mappings().all()
    tipos = db.execute(SQL_TIPOS_MED).mappings().all()

    # --- Validaciones básicas
    if not nombre:
        ctx = {"item": None, "categorias": categorias, "tipos_medicamento": tipos, "error": "El nombre es obligatorio"}
        return render_admin(templates, request, "admin_producto_form.html", ctx, admin_user)

    if db.execute(SQL_EXISTS_SLUG, {"slug": slug}).first():
        ctx = {"item": None, "categorias": categorias, "tipos_medicamento": tipos, "error": "Ya existe un producto con ese slug"}
        return render_admin(templates, request, "admin_producto_form.html", ctx, admin_user)

    if not db.execute(SQL_TIPO_MED_EXISTS, {"id": id_tipo_medicamento}).first():
        ctx = {"item": None, "categorias": categorias, "tipos_medicamento": tipos, "error": "Tipo de medicamento inválido"}
        return render_admin(templates, request, "admin_producto_form.html", ctx, admin_user)

    # --- Marca (usar id o crear por nombre)
    marca_id_val = _to_opt_int(marca_id_raw)
    if not marca_id_val:
        mn = (marca_nombre or "").strip()
        if mn:
            found = db.execute(SQL_MARCA_FIND_BY_NAME, {"nombre": mn}).first()
            if found:
                marca_id_val = int(found[0])
            else:
                new = db.execute(SQL_MARCA_INSERT, {"nombre": mn, "slug": _slugify(mn)}).first()
                marca_id_val = int(new[0])
                print(f"[POST nuevo] Marca creada '{mn}' id={marca_id_val}")

    # --- Insertar producto
    params = {
        "nombre": nombre,
        "slug": slug,
        "descripcion_web": descripcion_web,
        "seo_titulo": seo_titulo,
        "seo_descripcion": seo_descripcion,
        "imagen_principal_url": None,
        "visible_web": visible_val,
        "requiere_receta": requiere_receta,
        "peso_gramos": _to_int(peso_gramos),
        "alto_mm": _to_int(alto_mm),
        "ancho_mm": _to_int(ancho_mm),
        "largo_mm": _to_int(largo_mm),
        "categoria_id": categoria_id,
        "subcategoria_id": subcategoria_id,
        "id_marca": marca_id_val,
        "id_tipo_medicamento": int(id_tipo_medicamento),
        "costo_neto": _to_decimal(costo_neto),
        "costo_promedio": _to_decimal(costo_promedio),
        "costo_ultimo": _to_decimal(costo_ultimo),
    }
    print("[POST nuevo] params a insertar:", params)
    id_producto = db.execute(SQL_INSERT_RETURNING, params).scalar_one()
    db.commit()
    print("[POST nuevo] Producto insertado id_producto=", id_producto)

    # --- Imágenes por id
    urls = _save_images_by_id(imagenes or [], id_producto)
    if urls:
        db.execute(text("UPDATE public.productos SET imagen_principal_url = :u WHERE id_producto = :id"),
                   {"u": urls[0], "id": id_producto})
        db.commit()

    # --- Código de barras principal
    if (codigo_barra or "").strip():
        db.execute(SQL_CB_CLEAR_PRINCIPAL, {"id_producto": id_producto})
        db.execute(SQL_CB_INSERT, {"id_producto": id_producto, "codigo_barra": codigo_barra.strip()})
        db.commit()
        print("[POST nuevo] Código de barra principal:", codigo_barra.strip())

    # =====================================================
    # PUBLICACIÓN DE PRECIO SEGÚN LISTA ACTIVA DE LA SESIÓN
    # =====================================================
    try:
        # 1) Determinar lista activa desde cookie; default 'pts'
        lista_activa = request.cookies.get("lista_precio_activa")
        if lista_activa not in ("pvp", "pts"):
            lista_activa = "pts"
        actor = admin_user.get("username", "admin") if isinstance(admin_user, dict) else "admin"
        print(f"[POST nuevo] lista_activa={lista_activa}")

        if lista_activa == "pvp":
            # --- PVP MANUAL ---
            pvp_val = _to_int(pvp_bruto_manual)
            if pvp_val and pvp_val > 0:
                _publicar_pvp_manual(db, id_producto, pvp_val, actor)
                db.commit()
                print(f"✅ [PVP] publicado {pvp_val} (manual)")
            else:
                print("ℹ️ [PVP] No se publicó porque no ingresaste PVP manual.")
        else:
            # --- PTS AUTOMÁTICO: costo_neto + margen por tipo ---
            # Lookup costo y tipo
            row = db.execute(text("""
                SELECT p.costo_neto, coalesce(upper(tm.codigo),'') AS tipo_cod
                FROM public.productos p
                LEFT JOIN public.tipo_medicamento tm ON tm.id_tipo_medicamento = p.id_tipo_medicamento
                WHERE p.id_producto = :id
            """), {"id": id_producto}).mappings().first()

            if not row or row["costo_neto"] is None:
                print("⚠️ [PTS] No se pudo calcular: falta costo_neto.")
            else:
                from decimal import Decimal, ROUND_HALF_UP
                costo = Decimal(row["costo_neto"])
                tipo_cod = row["tipo_cod"] or ""

                # márgenes por tipo
                if "GEN" in tipo_cod:
                    m = Decimal("0.20")
                elif "BIO" in tipo_cod:
                    m = Decimal("0.05")
                elif "MARCA" in tipo_cod:
                    m = Decimal("0.03")
                else:
                    m = Decimal("0.08")

                pts_val = int((costo * (Decimal("1") + m)).to_integral_value(rounding=ROUND_HALF_UP))
                # publicar en lista 'pts'
                id_lista_pts = _get_lista_id_by_slug(db, "pts")
                db.execute(SQL_PRECIOS_CERRAR_VIG, {"id_producto": id_producto, "id_lista": id_lista_pts})
                rid = db.execute(SQL_PRECIOS_INSERT, {
                    "id_producto": id_producto,
                    "id_lista": id_lista_pts,
                    "precio_bruto": pts_val,
                    "creado_por": actor
                }).scalar_one()
                db.commit()
                print(f"✅ [PTS] publicado {pts_val} (calc) id_precio={rid}")
    except Exception as e:
        db.rollback()
        print(f"⚠️ [publicación precio][nuevo] error: {e!r}")

    return RedirectResponse(url="/admin/productos", status_code=303)

# -----------------
# EDITAR
# -----------------
@router.get("/admin/productos/{id_producto}/editar", response_class=HTMLResponse)
def admin_productos_edit_form(
    id_producto: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    item = db.execute(SQL_GET, {"id_producto": id_producto}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/productos", status_code=303)

    categorias = db.execute(SQL_CATEGORIAS_OPCIONES).mappings().all()
    tipos = db.execute(SQL_TIPOS_MED).mappings().all()

    lista_activa = request.cookies.get("lista_precio_activa")
    if lista_activa not in ("pvp", "pts"):
        lista_activa = "pts"

    ctx = {
        "item": item,
        "categorias": categorias,
        "tipos_medicamento": tipos,
        "lista_activa": lista_activa,
    }
    return render_admin(templates, request, "admin_producto_form.html", ctx, admin_user)

@router.post("/admin/productos/{id_producto}/editar")
def admin_productos_edit_submit(
    id_producto: int,
    request: Request,
    nombre: str = Form(...),
    laboratorio: str = Form(""),
    slug: str = Form(""),
    descripcion_web: str = Form(""),
    seo_titulo: str = Form(""),
    seo_descripcion: str = Form(""),
    visible_web: str = Form("false"),
    tipo_receta: str = Form("Venta Libre"),

    categoria_id_raw: str = Form(""),
    subcategoria_id_raw: str = Form(""),
    marca_id_raw: str = Form(""),
    marca_nombre: str = Form(""),

    id_tipo_medicamento: int = Form(...),

    codigo_barra: str = Form(""),

    # >>>>>> PVP manual y costos <<<<<<
    pvp_bruto_manual: str = Form(""),
    stock: str = Form(""),

    peso_gramos: str = Form(""),
    alto_mm: str = Form(""),
    ancho_mm: str = Form(""),
    largo_mm: str = Form(""),

    costo_neto: str = Form(""),
    costo_promedio: str = Form(""),
    costo_ultimo: str = Form(""),

    imagenes: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    print(f"[POST editar] id_producto={id_producto} nombre={nombre}")

    current = db.execute(SQL_GET, {"id_producto": id_producto}).mappings().first()
    if not current:
        return RedirectResponse(url="/admin/productos", status_code=303)

    if not db.execute(SQL_TIPO_MED_EXISTS, {"id": id_tipo_medicamento}).first():
        categorias = db.execute(SQL_CATEGORIAS_OPCIONES).mappings().all()
        tipos = db.execute(SQL_TIPOS_MED).mappings().all()
        ctx = {"item": current, "categorias": categorias, "tipos_medicamento": tipos, "error": "Tipo de medicamento inválido"}
        return render_admin(templates, request, "admin_producto_form.html", ctx, admin_user)

    nombre = (nombre or "").strip()
    slug = (slug or "").strip() or _slugify(nombre)
    visible_val = _bool_from_form(visible_web)
    requiere_receta = (str(tipo_receta).strip().lower() != "venta libre")
    categoria_id = _to_opt_int(categoria_id_raw)
    subcategoria_id = _to_opt_int(subcategoria_id_raw)

    # Marca
    marca_id_val = _to_opt_int(marca_id_raw)
    if not marca_id_val:
        mn = (marca_nombre or "").strip()
        if mn:
            found = db.execute(SQL_MARCA_FIND_BY_NAME, {"nombre": mn}).first()
            if found:
                marca_id_val = int(found[0])
            else:
                new = db.execute(SQL_MARCA_INSERT, {"nombre": mn, "slug": _slugify(mn)}).first()
                marca_id_val = int(new[0])
                print(f"[POST editar] Marca creada '{mn}' id={marca_id_val}")

    # Imágenes
    imagen_principal_url = current.get("imagen_principal_url")
    if imagenes:
        urls = _save_images_by_id(imagenes or [], id_producto)
        if urls:
            imagen_principal_url = urls[0]

    params = {
        "id_producto": id_producto,
        "nombre": nombre,
        "slug": slug,
        "descripcion_web": descripcion_web,
        "seo_titulo": seo_titulo,
        "seo_descripcion": seo_descripcion,
        "imagen_principal_url": imagen_principal_url,
        "visible_web": visible_val,
        "requiere_receta": requiere_receta,
        "peso_gramos": _to_int(peso_gramos),
        "alto_mm": _to_int(alto_mm),
        "ancho_mm": _to_int(ancho_mm),
        "largo_mm": _to_int(largo_mm),
        "categoria_id": categoria_id,
        "subcategoria_id": subcategoria_id,
        "id_marca": marca_id_val,
        "id_tipo_medicamento": int(id_tipo_medicamento),
        "costo_neto": _to_decimal(costo_neto),
        "costo_promedio": _to_decimal(costo_promedio),
        "costo_ultimo": _to_decimal(costo_ultimo),
    }
    print("[POST editar] params a actualizar:", params)
    db.execute(SQL_UPDATE, params)
    db.commit()
    print("[POST editar] OK id_producto=", id_producto)

    # Código de barras principal
    if (codigo_barra or "").strip():
        db.execute(SQL_CB_CLEAR_PRINCIPAL, {"id_producto": id_producto})
        db.execute(SQL_CB_INSERT, {"id_producto": id_producto, "codigo_barra": codigo_barra.strip()})
        db.commit()
        print("[POST editar] Código de barra principal:", codigo_barra.strip())

    # =====================================================
    # PUBLICACIÓN DE PRECIO SEGÚN LISTA ACTIVA DE LA SESIÓN
    # =====================================================
    try:
        # 1) Determinar lista activa desde cookie; default 'pts'
        lista_activa = request.cookies.get("lista_precio_activa")
        if lista_activa not in ("pvp", "pts"):
            lista_activa = "pts"
        actor = admin_user.get("username", "admin") if isinstance(admin_user, dict) else "admin"
        print(f"[POST editar] lista_activa={lista_activa}")

        if lista_activa == "pvp":
            # --- PVP MANUAL ---
            pvp_val = _to_int(pvp_bruto_manual)
            if pvp_val and pvp_val > 0:
                _publicar_pvp_manual(db, id_producto, pvp_val, actor)
                db.commit()
                print(f"✅ [PVP] publicado {pvp_val} (manual)")
            else:
                print("ℹ️ [PVP] No se publicó porque no ingresaste PVP manual.")
        else:
            # --- PTS AUTOMÁTICO: costo_neto + margen por tipo ---
            row = db.execute(text("""
                SELECT p.costo_neto, coalesce(upper(tm.codigo),'') AS tipo_cod
                FROM public.productos p
                LEFT JOIN public.tipo_medicamento tm ON tm.id_tipo_medicamento = p.id_tipo_medicamento
                WHERE p.id_producto = :id
            """), {"id": id_producto}).mappings().first()

            if not row or row["costo_neto"] is None:
                print("⚠️ [PTS] No se pudo calcular: falta costo_neto.")
            else:
                from decimal import Decimal, ROUND_HALF_UP
                costo = Decimal(row["costo_neto"])
                tipo_cod = row["tipo_cod"] or ""

                # márgenes por tipo (ajústalos si quieres)
                if "GEN" in tipo_cod:
                    m = Decimal("0.20")
                elif "BIO" in tipo_cod:
                    m = Decimal("0.05")
                elif "MARCA" in tipo_cod:
                    m = Decimal("0.03")
                else:
                    m = Decimal("0.08")

                pts_val = int((costo * (Decimal("1") + m)).to_integral_value(rounding=ROUND_HALF_UP))
                # publicar en lista 'pts'
                id_lista_pts = _get_lista_id_by_slug(db, "pts")
                db.execute(SQL_PRECIOS_CERRAR_VIG, {"id_producto": id_producto, "id_lista": id_lista_pts})
                rid = db.execute(SQL_PRECIOS_INSERT, {
                    "id_producto": id_producto,
                    "id_lista": id_lista_pts,
                    "precio_bruto": pts_val,
                    "creado_por": actor
                }).scalar_one()
                db.commit()
                print(f"✅ [PTS] publicado {pts_val} (calc) id_precio={rid}")
    except Exception as e:
        db.rollback()
        print(f"⚠️ [publicación precio][editar] error: {e!r}")

    return RedirectResponse(url="/admin/productos", status_code=303)

# -----------------
# ELIMINAR
# -----------------
@router.post("/admin/productos/{id_producto}/eliminar")
def admin_productos_delete(id_producto: int, _admin=Depends(require_admin), db: Session = Depends(get_db)):
    print("[DELETE] id_producto=", id_producto)
    db.execute(SQL_DELETE_ID, {"id_producto": id_producto})
    db.commit()
    return RedirectResponse(url="/admin/productos", status_code=303)

# -----------------
# CARGA MASIVA (CSV)
# -----------------
REQUIRED_COLS = [
    "codigo", "nombre",
    # opcionales (si vienen, se upsert-ean):
    "laboratorio", "slug", "descripcion_web", "imagen_principal_url",
    "seo_titulo", "seo_descripcion",
    "visible_web", "requiere_receta",
    "precio_venta", "stock",
    "peso_gramos", "alto_mm", "ancho_mm", "largo_mm",
]

@router.get("/admin/productos/carga", response_class=HTMLResponse)
def admin_productos_carga_form(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin_producto_carga.html",
        {
            "request": request,
            "columns": REQUIRED_COLS,
            "example": "codigo,nombre,laboratorio,precio_venta,stock,visible_web\nKIT500,Kitadol 500 mg,Bayer,1590,120,true",
            "user": admin_user,
        }
    )

@router.post("/admin/productos/carga")
async def admin_productos_carga_submit(
    request: Request,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    # Validaciones básicas
    if not archivo.filename.lower().endswith(".csv"):
        return templates.TemplateResponse("admin_producto_carga.html",
            {"request": request, "error": "Sube un archivo .csv", "columns": REQUIRED_COLS, "example": None},
            status_code=400)

    content = await archivo.read()
    try:
        text_data = content.decode("utf-8-sig")  # soporta BOM
    except UnicodeDecodeError:
        text_data = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text_data))
    header = [h.strip() for h in reader.fieldnames or []]
    missing = [c for c in ["codigo","nombre"] if c not in [x.lower() for x in header]]
    if missing:
        return templates.TemplateResponse("admin_producto_carga.html",
            {"request": request, "error": f"Faltan columnas obligatorias: {', '.join(missing)}",
             "columns": REQUIRED_COLS, "example": None},
            status_code=400)

    created = 0
    updated = 0
    errors = []

    for i, row in enumerate(reader, start=2):  # línea 2 = primera de datos
        try:
            # normalizar keys a lower
            r = { (k or "").strip().lower(): (v or "").strip() for k,v in row.items() }

            codigo = r.get("codigo") or ""
            nombre = r.get("nombre") or ""
            if not codigo or not nombre:
                raise ValueError("codigo y nombre son obligatorios")

            laboratorio = r.get("laboratorio", "")
            slug = r.get("slug") or _slugify(nombre)
            descripcion_web = r.get("descripcion_web", "")
            imagen_principal_url = r.get("imagen_principal_url", "")
            seo_titulo = r.get("seo_titulo", "")
            seo_descripcion = r.get("seo_descripcion", "")
            visible_web = (r.get("visible_web","").lower() in ("1","true","si","sí","yes","y"))
            requiere_receta = (r.get("requiere_receta","").lower() in ("1","true","si","sí","yes","y"))

            peso_gramos = _to_int(r.get("peso_gramos"))
            alto_mm = _to_int(r.get("alto_mm"))
            ancho_mm = _to_int(r.get("ancho_mm"))
            largo_mm = _to_int(r.get("largo_mm"))

            precio_venta = _to_float(r.get("precio_venta"))
            stock = _to_int(r.get("stock"))

            ex = db.execute(SQL_EXISTS, {"codigo": codigo}).first()
            params = {
                "codigo": codigo, "nombre": nombre, "laboratorio": laboratorio,
                "slug": slug, "descripcion_web": descripcion_web, "imagen_principal_url": imagen_principal_url,
                "seo_titulo": seo_titulo, "seo_descripcion": seo_descripcion,
                "visible_web": visible_web, "requiere_receta": requiere_receta,
                "peso_gramos": peso_gramos, "alto_mm": alto_mm, "ancho_mm": ancho_mm, "largo_mm": largo_mm,
            }

            if ex:
                db.execute(SQL_UPDATE, params)
                updated += 1
            else:
                db.execute(SQL_INSERT, params)
                created += 1

            if precio_venta is not None:
                db.execute(SQL_UPSERT_PRECIO, {"codigo": codigo, "precio": precio_venta})
            if stock is not None:
                db.execute(SQL_UPSERT_STOCK, {"codigo": codigo, "cantidad": stock})

        except Exception as e:
            errors.append(f"Línea {i}: {e}")

    db.commit()

    resumen = f"Creados: {created} · Actualizados: {updated} · Errores: {len(errors)}"
    return templates.TemplateResponse("admin_producto_carga.html", {
        "request": request,
        "ok": resumen,
        "errors": errors,
        "columns": REQUIRED_COLS,
        "example": None
    })

@router.get("/admin/subcategorias", response_class=JSONResponse)
def admin_subcategorias_list(
    id_categoria: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    rows = db.execute(SQL_SUBCATS_BY_CAT, {"id_categoria": id_categoria}).mappings().all()
    return {"ok": True, "items": [{"id": r["id_subcategoria"], "nombre": r["nombre"]} for r in rows]}

@router.post("/admin/subcategorias/nueva", response_class=JSONResponse)
def admin_subcategorias_new(
    id_categoria: int = Form(...),
    nombre: str = Form(...),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "El nombre es obligatorio"}, status_code=422)

    # Evitar duplicados (por categoría)
    found = db.execute(SQL_SUBCAT_FIND_BY_NAME, {"id_categoria": id_categoria, "nombre": nombre}).first()
    if found:
        return {"ok": True, "id_subcategoria": int(found[0]), "created": False}

    new = db.execute(SQL_SUBCAT_INSERT, {"id_categoria": id_categoria, "nombre": nombre, "slug": _slugify(nombre)}).first()
    db.commit()
    return {"ok": True, "id_subcategoria": int(new[0]), "created": True}

@router.get("/admin/precios/lista/usar")
def admin_precio_usar_lista(
    slug: Literal["pvp","pts"],
    request: Request,
    redirect: str = "/admin/productos",
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    # si quisieras validar que la lista existe en BD, descomenta:
    # _get_lista_id_by_slug(db, slug)
    resp = RedirectResponse(url=redirect, status_code=303)
    resp.set_cookie("lista_precio_activa", slug, max_age=60*60*6, httponly=True, samesite="lax")
    return resp
