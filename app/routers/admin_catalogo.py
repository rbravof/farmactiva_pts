# app/routers/admin_catalogo.py
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, Query, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
import os
import re
import unicodedata
from app.database import get_db
from app.routers.admin_security import require_admin
from app.utils.view import render_admin

UPLOAD_DIR = "static/uploads/marcas"  # asegúrate que exista y tenga permisos de escritura

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(
    tags=["Admin Catálogo"],
    dependencies=[Depends(require_admin)]  # ← proteger TODO este router
)

# -------- Utils ----------
def _slugify(s: str) -> str:
    s = (s or "").strip()
    # Normaliza (NFKD) y elimina marcas diacríticas
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    # Sustituye cualquier cosa no alfanumérica por guión
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "-"

def _save_logo(file: UploadFile, slug: str) -> str | None:
    if not file or not file.filename:
        return None

    os.makedirs("static/uploads/marcas", exist_ok=True)
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"]:
        ext = ".png"

    dest_rel = f"/static/uploads/marcas/{slug}{ext}"
    dest_abs = dest_rel.lstrip("/")  # elimina la / inicial para abrir como path local

    with open(dest_abs, "wb") as f:
        f.write(file.file.read())

    # Validar por MIME (opcional, más seguro que imghdr)
    if file.content_type and not file.content_type.startswith("image/"):
        os.remove(dest_abs)
        return None

    return dest_rel

# ========================
# CATEGORÍAS
# ========================
SQL_CAT_LIST = text("""
SELECT
  c.id,
  c.nombre,
  c.slug,
  c.visible,
  c.orden,
  COALESCE(SUM(CASE WHEN s.activo THEN 1 ELSE 0 END), 0) AS subcats_activas,
  COALESCE(COUNT(s.id_subcategoria), 0)                  AS subcats_total
FROM categorias c
LEFT JOIN subcategorias s ON s.id_categoria = c.id
GROUP BY c.id, c.nombre, c.slug, c.visible, c.orden
ORDER BY c.orden ASC, lower(c.nombre) ASC
""")

SQL_CAT_BY_ID = text("SELECT id, nombre, slug, visible, orden FROM categorias WHERE id = :id LIMIT 1")

SQL_CAT_EXISTS_SLUG = text("SELECT 1 FROM categorias WHERE lower(slug) = lower(:slug) AND (:id IS NULL OR id <> :id) LIMIT 1")

SQL_CAT_INSERT = text("""
    INSERT INTO categorias (nombre, slug, visible, orden)
    VALUES (:nombre, :slug, :visible, :orden)
""")

SQL_CAT_UPDATE = text("""
    UPDATE categorias
       SET nombre = :nombre,
           slug   = :slug,
           visible = :visible,
           orden  = :orden,
           updated_at = NOW()
     WHERE id = :id
""")

SQL_CAT_DELETE = text("DELETE FROM categorias WHERE id = :id")

# ========================
# SUB-CATEGORÍAS
# ========================
SQL_SUBCAT_LIST_BY_CAT = text("""
SELECT id_subcategoria AS id, nombre, slug, activo
FROM subcategorias
WHERE id_categoria = :id
ORDER BY lower(nombre)
""")

SQL_SUBCAT_EXISTS = text("""
  SELECT 1
  FROM subcategorias
  WHERE id_categoria = :id_categoria AND lower(slug) = lower(:slug)
  LIMIT 1
""")

SQL_SUBCAT_INSERT = text("""
  INSERT INTO subcategorias (id_categoria, nombre, slug, activo)
  VALUES (:id_categoria, :nombre, :slug, TRUE)
  RETURNING id_subcategoria
""")

SQL_SUBCAT_TOGGLE = text("""
  UPDATE subcategorias
     SET activo = NOT activo
   WHERE id_subcategoria = :id_subcategoria
""")

SQL_SUBCAT_DELETE = text("""
  DELETE FROM subcategorias
  WHERE id_subcategoria = :id_subcategoria
""")

@router.get("/admin/categorias", response_class=HTMLResponse)
def admin_categorias_list(
    request: Request,
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.execute(SQL_CAT_LIST).mappings().all()
    return templates.TemplateResponse(
        "admin_categoria_list.html",
        {"request": request, "rows": rows, "user": admin_user}
    )

@router.get("/admin/categorias/nueva", response_class=HTMLResponse)
def admin_categorias_new_form(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    return render_admin(templates, request, "admin_categoria_form.html", {"item": None}, admin_user)

from app.utils.view import render_admin

@router.post("/admin/categorias/nueva")
def admin_categorias_new_submit(
    request: Request,
    nombre: str = Form(...),
    slug: str = Form(""),
    visible: bool = Form(False),
    descripcion: str = Form(""),              # si agregaste descripción
    logo: UploadFile = File(None),            # ← importante
    orden: int = Form(0),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not slug:
        slug = _slugify(nombre)

    # Validación básica
    if not nombre or not slug:
        return render_admin(
            templates,
            request,
            "admin_categoria_form.html",
            {
                "item": {"nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
                "error": "Nombre y slug son obligatorios",
            },
            admin_user,
        )

    # Verificar duplicado
    exists = db.execute(SQL_CAT_EXISTS_SLUG, {"slug": slug, "id": None}).first()
    if exists:
        return render_admin(
            templates,
            request,
            "admin_categoria_form.html",
            {
                "item": {"nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
                "error": "Ya existe una categoría con ese slug",
            },
            admin_user,
        )

    # Insertar en DB
    db.execute(
        SQL_CAT_INSERT,
        {"nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
    )
    db.commit()
    return RedirectResponse(url="/admin/categorias", status_code=303)

@router.get("/admin/categorias/{id}/editar", response_class=HTMLResponse)
def admin_categorias_edit_form(
    id: int,
    request: Request,
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.execute(SQL_CAT_BY_ID, {"id": id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Categoría no encontrada")

    subcats = db.execute(SQL_SUBCAT_LIST_BY_CAT, {"id": id}).mappings().all()
    return render_admin(
        templates,
        request,
        "admin_categoria_form.html",
        {"item": dict(row), "subcategorias": subcats},
        admin_user,
    )

@router.post("/admin/categorias/{id}/editar")
def admin_categorias_edit_submit(
    id: int,
    request: Request,
    nombre: str = Form(...),
    slug: str = Form(""),
    visible: bool = Form(False),
    orden: int = Form(0),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not slug:
        slug = _slugify(nombre)

    # Validación básica
    if not nombre or not slug:
        return render_admin(
            templates,
            request,
            "admin_categoria_form.html",
            {
                "item": {"id": id, "nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
                "error": "Nombre y slug son obligatorios",
            },
            admin_user,
        )

    # Slug duplicado (en otra categoría)
    exists = db.execute(SQL_CAT_EXISTS_SLUG, {"slug": slug, "id": id}).first()
    if exists:
        return render_admin(
            templates,
            request,
            "admin_categoria_form.html",
            {
                "item": {"id": id, "nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
                "error": "Ya existe una categoría con ese slug",
            },
            admin_user,
        )

    # Update
    db.execute(
        SQL_CAT_UPDATE,
        {"id": id, "nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
    )
    db.commit()
    return RedirectResponse(url="/admin/categorias", status_code=303)

@router.post("/admin/categorias/{id}/subcategorias/nueva")
def admin_subcategorias_new_from_categoria(
    id: int,
    nombre: str = Form(...),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "El nombre es obligatorio"}, status_code=400)

    slug = _slugify(nombre)

    exists = db.execute(SQL_SUBCAT_EXISTS, {"id_categoria": id, "slug": slug}).first()
    if exists:
        id_sub = db.execute(text("""
            SELECT id_subcategoria
            FROM subcategorias
            WHERE id_categoria = :id_categoria AND slug = :slug
            LIMIT 1
        """), {"id_categoria": id, "slug": slug}).scalar()
        return JSONResponse({"ok": True, "id_subcategoria": id_sub, "nombre": nombre, "slug": slug, "created": False})

    rec = db.execute(SQL_SUBCAT_INSERT, {"id_categoria": id, "nombre": nombre, "slug": slug}).first()
    db.commit()
    return JSONResponse({"ok": True, "id_subcategoria": int(rec[0]), "nombre": nombre, "slug": slug, "created": True})

# --- Toggle visibilidad CATEGORÍAS ---
@router.post("/admin/categorias/{id}/toggle")
def admin_categorias_toggle(
    id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    db.execute(text("""
        UPDATE categorias
           SET visible = NOT visible,
               updated_at = NOW()
         WHERE id = :id
    """), {"id": id})
    db.commit()
    return RedirectResponse(url="/admin/categorias", status_code=303)

@router.post("/admin/categorias/{id}/eliminar")
def admin_categorias_delete(id: int, _admin=Depends(require_admin), db: Session = Depends(get_db)):
    db.execute(SQL_CAT_DELETE, {"id": id})
    db.commit()
    return RedirectResponse(url="/admin/categorias", status_code=303)

# ========================
# SUB-CATEGORIAS
# ========================
@router.post("/admin/categorias/{id}/subcategorias/nueva")
def admin_subcategorias_new(
    id: int,
    request: Request,
    nombre: str = Form(...),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    nombre = (nombre or "").strip()
    slug = _slugify(nombre)

    # Validación básica
    if not nombre:
        # Re-render con error y listado
        row = db.execute(SQL_CAT_BY_ID, {"id": id}).mappings().first()
        subcats = db.execute(SQL_SUBCAT_LIST_BY_CAT, {"id_categoria": id}).mappings().all()
        return render_admin(
            templates, request, "admin_categoria_form.html",
            {"item": dict(row), "subcategorias": subcats, "error": "El nombre es obligatorio"},
            admin_user,
        )

    # Evitar duplicados por (id_categoria, slug)
    exists = db.execute(SQL_SUBCAT_EXISTS, {"id_categoria": id, "slug": slug}).first()
    if not exists:
        db.execute(SQL_SUBCAT_INSERT, {"id_categoria": id, "nombre": nombre, "slug": slug})
        db.commit()

    return RedirectResponse(url=f"/admin/categorias/{id}/editar", status_code=303)

@router.post("/admin/subcategorias/{id_sub}/toggle")
def admin_subcategorias_toggle(
    id_sub: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    db.execute(SQL_SUBCAT_TOGGLE, {"id_subcategoria": id_sub})
    db.commit()
    ref = request.headers.get("referer") or "/admin/categorias"
    return RedirectResponse(url=ref, status_code=303)

@router.post("/admin/subcategorias/{id_sub}/eliminar")
def admin_subcategorias_delete(
    id_sub: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    db.execute(SQL_SUBCAT_DELETE, {"id_subcategoria": id_sub})
    db.commit()
    ref = request.headers.get("referer") or "/admin/categorias"
    return RedirectResponse(url=ref, status_code=303)

@router.post("/admin/subcategorias/{id_sub}/actualizar")
def admin_subcategorias_actualizar(
    id_sub: int,
    nombre: str = Form(...),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "El nombre es obligatorio"}, status_code=400)

    slug = _slugify(nombre)

    row = db.execute(
        text("SELECT id_categoria FROM subcategorias WHERE id_subcategoria = :id"),
        {"id": id_sub}
    ).mappings().first()
    if not row:
        return JSONResponse({"ok": False, "error": "Subcategoría no encontrada"}, status_code=404)

    # Evitar duplicados dentro de la misma categoría
    exists = db.execute(text("""
        SELECT 1
        FROM subcategorias
        WHERE id_categoria = :id_categoria
          AND slug = :slug
          AND id_subcategoria <> :id_sub
        LIMIT 1
    """), {"id_categoria": row["id_categoria"], "slug": slug, "id_sub": id_sub}).first()
    if exists:
        return JSONResponse({"ok": False, "error": "Ya existe una subcategoría con ese nombre en esta categoría."}, status_code=409)

    db.execute(text("""
        UPDATE subcategorias
           SET nombre = :nombre, slug = :slug
         WHERE id_subcategoria = :id_sub
    """), {"nombre": nombre, "slug": slug, "id_sub": id_sub})
    db.commit()

    return JSONResponse({"ok": True, "id_subcategoria": id_sub, "nombre": nombre, "slug": slug})

@router.get("/admin/subcategorias")
def admin_subcategorias_by_categoria(
    id_categoria: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    rows = db.execute(text("""
        SELECT id_subcategoria AS id, nombre, slug, activo
        FROM subcategorias
        WHERE id_categoria = :id_categoria
        ORDER BY lower(nombre)
    """), {"id_categoria": id_categoria}).mappings().all()
    return JSONResponse({"ok": True, "items": [dict(r) for r in rows]})

@router.post("/admin/subcategorias/nueva")
def admin_subcategorias_new_global(
    id_categoria: int = Form(...),
    nombre: str = Form(...),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "El nombre es obligatorio"}, status_code=400)

    slug = _slugify(nombre)

    exists = db.execute(SQL_SUBCAT_EXISTS, {"id_categoria": id_categoria, "slug": slug}).first()
    if exists:
        id_sub = db.execute(text("""
            SELECT id_subcategoria
            FROM subcategorias
            WHERE id_categoria = :id_categoria AND slug = :slug
            LIMIT 1
        """), {"id_categoria": id_categoria, "slug": slug}).scalar()
        return JSONResponse({"ok": True, "id_subcategoria": id_sub, "nombre": nombre, "slug": slug, "created": False})

    rec = db.execute(SQL_SUBCAT_INSERT, {"id_categoria": id_categoria, "nombre": nombre, "slug": slug}).first()
    db.commit()
    return JSONResponse({"ok": True, "id_subcategoria": int(rec[0]), "nombre": nombre, "slug": slug, "created": True})

# ========================
# MARCAS
# ========================
SQL_BRAND_LIST = text("""
    SELECT id, nombre, slug, visible, orden, logo_url
    FROM marcas
    ORDER BY visible DESC, orden ASC, lower(nombre) ASC
""")

SQL_BRAND_BY_ID = text("SELECT id, nombre, slug, visible, orden, logo_url FROM marcas WHERE id = :id LIMIT 1")

SQL_BRAND_EXISTS_SLUG = text("SELECT 1 FROM marcas WHERE lower(slug) = lower(:slug) AND (:id IS NULL OR id <> :id) LIMIT 1")

SQL_BRAND_INSERT = text("""
    INSERT INTO marcas (nombre, slug, visible, orden, logo_url)
    VALUES (:nombre, :slug, :visible, :orden, :logo_url)
""")

SQL_BRAND_UPDATE = text("""
    UPDATE marcas
       SET nombre             = :nombre,
           slug               = :slug,
           visible            = :visible,
           orden              = :orden,
           logo_url           = :logo_url,
           fecha_actualizacion = NOW()
     WHERE id = :id
""")

SQL_BRAND_DELETE = text("DELETE FROM marcas WHERE id = :id")

@router.get("/admin/marcas", response_class=HTMLResponse)
def admin_marcas_list(
    request: Request,
    admin_user: dict = Depends(require_admin),   # ← user autenticado y con rol admin
    db: Session = Depends(get_db),
):
    # (opcional) traza de diagnóstico
    dbname = db.execute(text("SELECT current_database()")).scalar()
    print(f"🧭 [ADMIN/MARCAS] DB actual = {dbname}")

    rows = db.execute(SQL_BRAND_LIST).mappings().all()
    # Pasa 'user' al contexto para que base_admin.html pueda mostrar el saludo
    return render_admin(templates, request, "admin_marca_list.html", {"rows": rows}, admin_user)

@router.get("/admin/marcas/nueva", response_class=HTMLResponse)
def admin_marcas_new_form(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    # Usar siempre la firma: render_admin(templates, request, "tpl.html", {ctx}, user)
    return render_admin(
        templates,
        request,
        "admin_marca_form.html",
        {"item": None},
        admin_user,
    )

@router.post("/admin/marcas/nueva")
def admin_marcas_new_submit(
    request: Request,
    nombre: str = Form(...),
    slug: str = Form(""),
    visible: bool = Form(False),
    orden: int = Form(0),
    logo: UploadFile = File(None),                 # ← NUEVO
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not slug:
        slug = _slugify(nombre)

    if not nombre or not slug:
        return render_admin(
            templates, request, "admin_marca_form.html",
            {
                "item": {"nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
                "error": "Nombre y slug son obligatorios",
            },
            admin_user,
        )

    exists = db.execute(SQL_BRAND_EXISTS_SLUG, {"slug": slug, "id": None}).first()
    if exists:
        return render_admin(
            templates, request, "admin_marca_form.html",
            {
                "item": {"nombre": nombre, "slug": slug, "visible": visible, "orden": orden},
                "error": "Ya existe una marca con ese slug",
            },
            admin_user,
        )

    # 🚀 Guardar logo si viene archivo
    logo_url = None
    if logo and logo.filename:
        os.makedirs("static/uploads/marcas", exist_ok=True)
        _, ext = os.path.splitext(logo.filename.lower())
        if ext not in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"]:
            ext = ".png"
        dest_rel = f"/static/uploads/marcas/{slug}{ext}"
        dest_abs = dest_rel.lstrip("/")
        with open(dest_abs, "wb") as f:
            f.write(logo.file.read())
        logo_url = dest_rel

    db.execute(SQL_BRAND_INSERT, {
        "nombre": nombre,
        "slug": slug,
        "visible": visible,
        "orden": orden,
        "logo_url": logo_url,                      # ← NUEVO
    })
    db.commit()
    return RedirectResponse(url="/admin/marcas", status_code=303)

@router.get("/admin/marcas/{id}/editar", response_class=HTMLResponse)
def admin_marcas_edit_form(
    id: int,
    request: Request,
    admin_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.execute(SQL_BRAND_BY_ID, {"id": id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Marca no encontrada")

    return render_admin(
        templates,
        request,
        "admin_marca_form.html",
        {"item": dict(row)},
        admin_user,
    )

@router.post("/admin/marcas/{id}/editar")
def admin_marcas_edit_submit(
    id: int,
    request: Request,
    nombre: str = Form(...),
    slug: str = Form(""),
    visible: bool = Form(False),
    orden: int = Form(0),
    remove_logo: bool = Form(False),              # ← NUEVO
    logo: UploadFile = File(None),                # ← NUEVO
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    nombre = (nombre or "").strip()
    if not slug:
        slug = _slugify(nombre)

    if not nombre or not slug:
        return render_admin(
            templates, request, "admin_marca_form.html",
            {
                "item": {"id": id, "nombre": nombre, "slug": slug,
                         "visible": visible, "orden": orden},
                "error": "Nombre y slug son obligatorios"
            },
            admin_user,
        )

    exists = db.execute(SQL_BRAND_EXISTS_SLUG, {"slug": slug, "id": id}).first()
    if exists:
        return render_admin(
            templates, request, "admin_marca_form.html",
            {
                "item": {"id": id, "nombre": nombre, "slug": slug,
                         "visible": visible, "orden": orden},
                "error": "Ya existe una marca con ese slug"
            },
            admin_user,
        )

    # Obtener la marca actual
    row = db.execute(SQL_BRAND_BY_ID, {"id": id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Marca no encontrada")

    current_logo = row.get("logo_url")
    new_logo_url = current_logo

    # Quitar logo
    if remove_logo and current_logo:
        try:
            os.remove(current_logo.lstrip("/"))
        except Exception:
            pass
        new_logo_url = None

    # Subir logo nuevo
    if logo and logo.filename:
        os.makedirs("static/uploads/marcas", exist_ok=True)
        _, ext = os.path.splitext(logo.filename.lower())
        if ext not in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"]:
            ext = ".png"
        dest_rel = f"/static/uploads/marcas/{slug}{ext}"
        dest_abs = dest_rel.lstrip("/")
        with open(dest_abs, "wb") as f:
            f.write(logo.file.read())
        new_logo_url = dest_rel

    db.execute(SQL_BRAND_UPDATE, {
        "id": id,
        "nombre": nombre,
        "slug": slug,
        "visible": visible,
        "orden": orden,
        "logo_url": new_logo_url,                   # ← NUEVO
    })
    db.commit()
    return RedirectResponse(url="/admin/marcas", status_code=303)

@router.post("/admin/marcas/{id}/eliminar")
def admin_marcas_delete(id: int, _admin=Depends(require_admin), db: Session = Depends(get_db)):
    db.execute(SQL_BRAND_DELETE, {"id": id})
    db.commit()
    return RedirectResponse(url="/admin/marcas", status_code=303)

# --- Toggle visibilidad MARCAS ---
@router.post("/admin/marcas/{id}/toggle")
def admin_marcas_toggle(
    id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    db.execute(text("""
        UPDATE marcas
           SET visible = NOT visible,
               fecha_actualizacion = NOW()
         WHERE id = :id
    """), {"id": id})
    db.commit()
    return RedirectResponse(url="/admin/marcas", status_code=303)
