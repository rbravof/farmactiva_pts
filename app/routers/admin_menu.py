# app/routers/admin_menu.py
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.admin_security import require_admin  # guard

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

# ---------- SQL BASE ----------
SQL_CATS = text("""
SELECT id, nombre
FROM public.categorias
ORDER BY nombre
""")

SQL_LIST = text("""
SELECT i.id_item, i.menu, i.label, i.tipo, i.url, i.categoria_id, i.subcategoria_id,
       i.parent_id, i.orden, i.visible, i.target_blank,
       p.label AS parent_label,
       c.nombre AS categoria_nombre,
       sc.nombre AS subcategoria_nombre
FROM public.web_menu_items i
LEFT JOIN public.web_menu_items p ON p.id_item = i.parent_id
LEFT JOIN public.categorias c     ON c.id = i.categoria_id
LEFT JOIN public.subcategorias sc ON sc.id_subcategoria = i.subcategoria_id
ORDER BY i.menu, COALESCE(i.parent_id, 0), i.orden, i.id_item
""")

SQL_GET = text("""
SELECT i.*
FROM public.web_menu_items i
WHERE i.id_item = :id_item
""")

SQL_PARENTS_FOR_MENU = text("""
SELECT id_item, label
FROM public.web_menu_items
WHERE menu = :menu
  AND ( :exclude_id IS NULL OR id_item <> :exclude_id )
ORDER BY COALESCE(parent_id,0), orden, id_item
""")

SQL_INSERT = text("""
INSERT INTO public.web_menu_items
(menu, label, tipo, url, categoria_id, subcategoria_id, parent_id, orden, visible, target_blank)
VALUES (:menu, :label, :tipo, :url, :categoria_id, :subcategoria_id, :parent_id, :orden, :visible, :target_blank)
RETURNING id_item
""")

SQL_UPDATE = text("""
UPDATE public.web_menu_items
SET menu=:menu, label=:label, tipo=:tipo, url=:url, categoria_id=:categoria_id,
    subcategoria_id=:subcategoria_id, parent_id=:parent_id, orden=:orden,
    visible=:visible, target_blank=:target_blank, actualizado_en=now()
WHERE id_item=:id_item
""")

SQL_DELETE = text("DELETE FROM public.web_menu_items WHERE id_item = :id_item")

# ---------- HELPERS ----------
def _bool(v: str) -> bool:
    return str(v or "").lower() in ("1", "true", "on", "si", "sí")

def render_admin(request: Request, name: str, ctx: dict):
    ctx = {**ctx, "request": request}
    return templates.TemplateResponse(name, ctx)

# =====================================================================
# VISTAS HTML (las que ya usabas)
# =====================================================================
# Lista completa (ambos menús) con los campos mínimos para armar el árbol
SQL_MENU_ITEMS_ALL = text("""
SELECT id_item, menu, label, tipo, url,
       categoria_id, subcategoria_id,
       parent_id, COALESCE(orden,0) AS orden,
       visible, target_blank
FROM public.web_menu_items
ORDER BY menu, COALESCE(parent_id,0), orden, id_item
""")

def _flatten_with_depth(rows):
    """Convierte lista plana en árbol y la aplana con 'depth' e incluye parent_label."""
    id2label = {r["id_item"]: r["label"] for r in rows}
    by_parent = {}
    for r in rows:
        d = dict(r)
        by_parent.setdefault(d.get("parent_id"), []).append(d)

    # orden de hermanos predecible
    for lst in by_parent.values():
        lst.sort(key=lambda x: (x.get("orden", 0), (x.get("label") or "").lower()))

    out = []

    def walk(parent_id, depth):
        for n in by_parent.get(parent_id, []):
            n["depth"] = depth
            n["parent_label"] = id2label.get(n.get("parent_id"))
            out.append(n)
            walk(n["id_item"], depth + 1)

    walk(None, 0)
    return out

@router.get("/admin/menu", dependencies=[Depends(require_admin)])
def admin_menu_list(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    # Trae todo y agrupa por menú
    rows_all = db.execute(SQL_MENU_ITEMS_ALL).mappings().all()
    by_menu = {}
    for r in rows_all:
        by_menu.setdefault(r["menu"], []).append(r)

    # Diccionarios de nombres para mostrar en "Destino"
    cats = {r["id"]: r["nombre"] for r in db.execute(SQL_CATS).mappings().all()}
    subcats = {
        r["id_subcategoria"]: r["nombre"]
        for r in db.execute(text("SELECT id_subcategoria, nombre FROM public.subcategorias")).mappings().all()
    }

    grouped = {}
    for menu_name, rows in by_menu.items():
        items = _flatten_with_depth(rows)
        # completa nombres de destino para la plantilla
        for it in items:
            if it["tipo"] == "categoria":
                it["categoria_nombre"] = cats.get(it["categoria_id"])
            elif it["tipo"] == "subcategoria":
                it["subcategoria_nombre"] = subcats.get(it["subcategoria_id"])
        grouped[menu_name] = items

    return render_admin(request, "admin_menu_list.html", {
        "grouped": grouped,
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
    })

@router.get("/admin/menu/nuevo", dependencies=[Depends(require_admin)])
def admin_menu_new_form(request: Request, menu: str = "header",
                        db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    categorias = db.execute(SQL_CATS).mappings().all()
    parents = db.execute(SQL_PARENTS_FOR_MENU, {"menu": menu, "exclude_id": None}).mappings().all()
    return render_admin(request, "admin_menu_form.html", {
        "mode": "new",
        "item": None,
        "menu_sel": menu,
        "categorias": categorias,
        "parents": parents,
    })

@router.get("/admin/menu/{id_item}/editar", dependencies=[Depends(require_admin)])
def admin_menu_edit_form(id_item: int, request: Request,
                         db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    item = db.execute(SQL_GET, {"id_item": id_item}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/menu?err=not_found", status_code=303)
    categorias = db.execute(SQL_CATS).mappings().all()
    parents = db.execute(SQL_PARENTS_FOR_MENU,
                         {"menu": item["menu"], "exclude_id": id_item}).mappings().all()
    return render_admin(request, "admin_menu_form.html", {
        "mode": "edit",
        "item": item,
        "menu_sel": item["menu"],
        "categorias": categorias,
        "parents": parents,
    })

@router.post("/admin/menu/nuevo", dependencies=[Depends(require_admin)])
def admin_menu_new_submit(
    request: Request,
    menu: str = Form("header"),
    label: str = Form(...),
    tipo: str = Form("url"),
    url: str = Form(""),
    categoria_id: str = Form(""),
    subcategoria_id: str = Form(""),
    parent_id: str = Form(""),
    orden: str = Form("0"),
    visible: str = Form("true"),
    target_blank: str = Form("false"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    categoria_id_val = int(categoria_id) if str(categoria_id).strip().isdigit() else None
    subcategoria_id_val = int(subcategoria_id) if str(subcategoria_id).strip().isdigit() else None
    parent_id_val = int(parent_id) if str(parent_id).strip().isdigit() else None
    orden_val = int(orden) if str(orden).strip().isdigit() else 0
    params = {
        "menu": menu.strip() or "header",
        "label": (label or "").strip(),
        "tipo": (tipo or "url").strip(),
        "url": (url or "").strip() if tipo == "url" else None,
        "categoria_id": categoria_id_val if tipo == "categoria" else None,
        "subcategoria_id": subcategoria_id_val if tipo == "subcategoria" else None,
        "parent_id": parent_id_val,
        "orden": orden_val,
        "visible": _bool(visible),
        "target_blank": _bool(target_blank),
    }
    db.execute(SQL_INSERT, params)
    db.commit()
    return RedirectResponse(url="/admin/menu?ok=created", status_code=303)

@router.post("/admin/menu/{id_item}/editar", dependencies=[Depends(require_admin)])
def admin_menu_edit_submit(
    id_item: int,
    request: Request,
    menu: str = Form("header"),
    label: str = Form(...),
    tipo: str = Form("url"),
    url: str = Form(""),
    categoria_id: str = Form(""),
    subcategoria_id: str = Form(""),
    parent_id: str = Form(""),
    orden: str = Form("0"),
    visible: str = Form("true"),
    target_blank: str = Form("false"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    categoria_id_val = int(categoria_id) if str(categoria_id).strip().isdigit() else None
    subcategoria_id_val = int(subcategoria_id) if str(subcategoria_id).strip().isdigit() else None
    parent_id_val = int(parent_id) if str(parent_id).strip().isdigit() else None
    orden_val = int(orden) if str(orden).strip().isdigit() else 0
    params = {
        "id_item": id_item,
        "menu": menu.strip() or "header",
        "label": (label or "").strip(),
        "tipo": (tipo or "url").strip(),
        "url": (url or "").strip() if tipo == "url" else None,
        "categoria_id": categoria_id_val if tipo == "categoria" else None,
        "subcategoria_id": subcategoria_id_val if tipo == "subcategoria" else None,
        "parent_id": parent_id_val,
        "orden": orden_val,
        "visible": _bool(visible),
        "target_blank": _bool(target_blank),
    }
    db.execute(SQL_UPDATE, params)
    db.commit()
    return RedirectResponse(url="/admin/menu?ok=updated", status_code=303)

@router.post("/admin/menu/{id_item}/eliminar", dependencies=[Depends(require_admin)])
def admin_menu_delete(id_item: int, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    db.execute(SQL_DELETE, {"id_item": id_item})
    db.commit()
    return RedirectResponse(url="/admin/menu?ok=deleted", status_code=303)

@router.post("/admin/menu/{id_item}/importar-subcategorias", dependencies=[Depends(require_admin)])
def admin_menu_import_subcats(id_item: int, db: Session = Depends(get_db), admin_user: dict = Depends(require_admin)):
    item = db.execute(SQL_GET, {"id_item": id_item}).mappings().first()
    if not item:
        return RedirectResponse(url="/admin/menu?err=not_found", status_code=303)
    if item["tipo"] != "categoria" or not item["categoria_id"]:
        return RedirectResponse(url=f"/admin/menu/{id_item}/editar?err=not_categoria", status_code=303)

    SQL_BULK = text("""
    WITH ordered AS (
      SELECT s.id_subcategoria, s.nombre,
             ROW_NUMBER() OVER (ORDER BY s.nombre) - 1 AS ord
      FROM public.subcategorias s
      WHERE s.id_categoria = :cat_id
    )
    INSERT INTO public.web_menu_items
      (menu, label, tipo, url, categoria_id, subcategoria_id, parent_id, orden, visible, target_blank)
    SELECT :menu, o.nombre, 'subcategoria', NULL, NULL, o.id_subcategoria, :parent_id, o.ord, TRUE, FALSE
    FROM ordered o
    LEFT JOIN public.web_menu_items w
      ON w.parent_id = :parent_id
     AND w.tipo = 'subcategoria'
     AND w.subcategoria_id = o.id_subcategoria
    WHERE w.id_item IS NULL;
    """)
    db.execute(SQL_BULK, {"cat_id": item["categoria_id"], "menu": item["menu"], "parent_id": id_item})
    db.commit()
    return RedirectResponse(url=f"/admin/menu/{id_item}/editar?ok=subcats_imported", status_code=303)

# =====================================================================
# API JSON para el constructor (lo que faltaba y causaba el 404)
# =====================================================================

SQL_LIST_BY_MENU = text("""
SELECT i.id_item, i.menu, i.label, i.tipo, i.url, i.categoria_id, i.subcategoria_id,
       i.parent_id, i.orden, i.visible, i.target_blank,
       c.nombre AS categoria_nombre,
       sc.nombre AS subcategoria_nombre
FROM public.web_menu_items i
LEFT JOIN public.categorias c     ON c.id = i.categoria_id
LEFT JOIN public.subcategorias sc ON sc.id_subcategoria = i.subcategoria_id
WHERE i.menu = :menu
ORDER BY COALESCE(i.parent_id, 0), i.orden, i.id_item
""")

def _row_to_node(r) -> Dict[str, Any]:
    # "destino" legible para el UI
    if r["tipo"] == "url":
        destino = r["url"] or ""
    elif r["tipo"] == "categoria":
        destino = f"Categoría: {r['categoria_nombre'] or r['categoria_id']}"
    elif r["tipo"] == "subcategoria":
        destino = f"Subcategoría: {r['subcategoria_nombre'] or r['subcategoria_id']}"
    else:
        destino = ""
    return {
        "id": r["id_item"],
        "menu": r["menu"],
        "label": r["label"],
        "tipo": r["tipo"],
        "url": r["url"],
        "categoria_id": r["categoria_id"],
        "subcategoria_id": r["subcategoria_id"],
        "parent_id": r["parent_id"],
        "orden": r["orden"],
        "visible": bool(r["visible"]),
        "target_blank": bool(r["target_blank"]),
        "destino": destino,
        "children": []
    }

@router.get("/admin/api/menu", dependencies=[Depends(require_admin)])
def api_menu_get(menu: str, db: Session = Depends(get_db)):
    rows = db.execute(SQL_LIST_BY_MENU, {"menu": menu}).mappings().all()
    nodes = {r["id_item"]: _row_to_node(r) for r in rows}
    roots: List[Dict[str, Any]] = []
    for r in rows:
        n = nodes[r["id_item"]]
        pid = r["parent_id"]
        if pid and pid in nodes:
            nodes[pid]["children"].append(n)
        else:
            roots.append(n)
    return JSONResponse(roots)

@router.post("/admin/api/menu/item", dependencies=[Depends(require_admin)])
async def api_menu_create_item(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    menu = (data.get("menu") or "header").strip()
    parent_id = data.get("parent_id")
    # calcula orden = max + 1 dentro de ese parent
    q_max = text("""
        SELECT COALESCE(MAX(orden)+1, 0) AS next
        FROM public.web_menu_items
        WHERE menu = :menu AND COALESCE(parent_id,0) = COALESCE(:pid,0)
    """)
    next_ord = db.execute(q_max, {"menu": menu, "pid": parent_id}).scalar() or 0

    params = {
        "menu": menu,
        "label": (data.get("label") or "").strip(),
        "tipo": (data.get("tipo") or "url").strip(),
        "url": (data.get("url") or "").strip() if (data.get("tipo") == "url") else None,
        "categoria_id": int(data["categoria_id"]) if (data.get("tipo") == "categoria" and str(data.get("categoria_id","")).isdigit()) else None,
        "subcategoria_id": int(data["subcategoria_id"]) if (data.get("tipo") == "subcategoria" and str(data.get("subcategoria_id","")).isdigit()) else None,
        "parent_id": int(parent_id) if str(parent_id or "").isdigit() else None,
        "orden": int(next_ord),
        "visible": bool(data.get("visible", True)),
        "target_blank": bool(data.get("target_blank", False)),
    }
    new_id = db.execute(SQL_INSERT, params).scalar_one()
    db.commit()
    return JSONResponse({"ok": True, "id": new_id})

@router.patch("/admin/api/menu/item/{id_item}", dependencies=[Depends(require_admin)])
async def api_menu_update_item(id_item: int, request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    current = db.execute(SQL_GET, {"id_item": id_item}).mappings().first()
    if not current:
        return JSONResponse({"error": "not_found"}, status_code=404)

    params = {
        "id_item": id_item,
        "menu": (data.get("menu") or current["menu"]).strip(),
        "label": (data.get("label") or current["label"]).strip(),
        "tipo": (data.get("tipo") or current["tipo"]).strip(),
        "url": (data.get("url") or (current["url"] or "")).strip() if (data.get("tipo", current["tipo"]) == "url") else None,
        "categoria_id": int(data["categoria_id"]) if (data.get("tipo", current["tipo"]) == "categoria" and str(data.get("categoria_id","")).isdigit()) else None,
        "subcategoria_id": int(data["subcategoria_id"]) if (data.get("tipo", current["tipo"]) == "subcategoria" and str(data.get("subcategoria_id","")).isdigit()) else None,
        "parent_id": int(data["parent_id"]) if str(data.get("parent_id","")).isdigit() else current["parent_id"],
        "orden": int(data["orden"]) if str(data.get("orden","")).isdigit() else current["orden"],
        "visible": bool(data.get("visible", current["visible"])),
        "target_blank": bool(data.get("target_blank", current["target_blank"])),
    }
    db.execute(SQL_UPDATE, params)
    db.commit()
    return JSONResponse({"ok": True})

@router.delete("/admin/api/menu/item/{id_item}", dependencies=[Depends(require_admin)])
def api_menu_delete_item(id_item: int, db: Session = Depends(get_db)):
    # borra subárbol con CTE recursiva por si no hay cascade
    SQL_DEL_TREE = text("""
    WITH RECURSIVE t AS (
      SELECT id_item FROM public.web_menu_items WHERE id_item = :id
      UNION ALL
      SELECT c.id_item
      FROM public.web_menu_items c
      JOIN t ON c.parent_id = t.id_item
    )
    DELETE FROM public.web_menu_items WHERE id_item IN (SELECT id_item FROM t)
    """)
    db.execute(SQL_DEL_TREE, {"id": id_item})
    db.commit()
    return JSONResponse({"ok": True})

@router.post("/admin/api/menu/reorder", dependencies=[Depends(require_admin)])
async def api_menu_reorder(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    menu = (data.get("menu") or "header").strip()
    items: List[Dict[str, Any]] = data.get("items") or []
    SQL_REORDER = text("""
    UPDATE public.web_menu_items
    SET parent_id=:parent_id, orden=:orden, label=:label, visible=:visible, actualizado_en=now()
    WHERE id_item = :id AND menu = :menu
    """)
    for it in items:
        db.execute(SQL_REORDER, {
            "id": it["id"],
            "menu": menu,
            "parent_id": it.get("parent_id"),
            "orden": it.get("orden", 0),
            "label": it.get("label", ""),
            "visible": bool(it.get("visible", True)),
        })
    db.commit()
    return JSONResponse({"ok": True})

def _flatten_with_depth(rows):
    """Convierte lista plana en árbol ordenado y la aplana con depth."""
    # mapear por parent
    by_parent = {}
    for r in rows:
        d = dict(r)
        by_parent.setdefault(d.get("parent_id"), []).append(d)

    # ordenar hermanos
    for lst in by_parent.values():
        lst.sort(key=lambda x: (x.get("orden", 0), (x.get("label") or "").lower()))

    out = []

    def walk(parent_id, depth):
        for n in by_parent.get(parent_id, []):
            n["depth"] = depth
            out.append(n)
            walk(n["id_item"], depth + 1)

    # raíz: parent_id NULL
    walk(None, 0)
    return out

