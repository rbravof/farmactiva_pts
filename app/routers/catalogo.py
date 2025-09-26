# app/routers/catalogo.py
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.security import get_current_user

router = APIRouter(prefix="/api/tienda", tags=["Tienda"])

# --- Ping básico ---
@router.get("/ping")
def ping():
    return {"mensaje": "pong desde catálogo"}

# ======================================================================
# BÚSQUEDA DE PRODUCTOS
# ======================================================================

# Consulta base: ajusta nombres de tabla/columnas según tu esquema real.
SQL_COUNT = """
SELECT COUNT(*)::bigint AS total
FROM productos p
WHERE 1=1
  {filtro_stock}
  {filtro_q}
"""

SQL_SELECT = """
SELECT
    p.codigo,
    p.nombre,
    p.laboratorio,
    p.precio_venta,
    p.imagen_url,
    COALESCE(p.stock, 0) AS stock
FROM productos p
WHERE 1=1
  {filtro_stock}
  {filtro_q}
ORDER BY p.nombre ASC
LIMIT :limit OFFSET :offset
"""

def _mk_filters(q: Optional[str], solo_con_stock: bool):
    filtro_stock = "AND COALESCE(p.stock, 0) > 0" if solo_con_stock else ""
    filtro_q = ""
    params = {}
    if q:
        # Busca por nombre y laboratorio (case-insensitive)
        filtro_q = "AND (p.nombre ILIKE :q OR p.laboratorio ILIKE :q)"
        params["q"] = f"%{q.strip()}%"
    return filtro_stock, filtro_q, params

def _map_item(row: dict) -> dict:
    """Normaliza el item al contrato esperado por el front."""
    return {
        "codigo": row.get("codigo"),
        "nombre": row.get("nombre") or "",
        "laboratorio": row.get("laboratorio") or "",
        "precio_venta": float(row.get("precio_venta") or 0),
        "imagen_url": row.get("imagen_url") or None,
        "stock": int(row.get("stock") or 0),
    }

@router.get("/buscar")
def buscar_productos(
    q: Optional[str] = Query(None, description="Texto de búsqueda (nombre/laboratorio)"),
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    solo_con_stock: bool = Query(True),
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),  # 🔒 exige login
):
    """Devuelve items y total según la búsqueda."""
    filtro_stock, filtro_q, params = _mk_filters(q, solo_con_stock)
    params.update({"limit": limit, "offset": offset})

    # Total
    count_sql = text(SQL_COUNT.format(filtro_stock=filtro_stock, filtro_q=filtro_q))
    total = db.execute(count_sql, params).scalar() or 0

    # Items
    select_sql = text(SQL_SELECT.format(filtro_stock=filtro_stock, filtro_q=filtro_q))
    rows = db.execute(select_sql, params).mappings().all()
    items = [_map_item(dict(r)) for r in rows]

    return {"items": items, "total": int(total), "limit": limit, "offset": offset}

# ======================================================================
# DETALLE DE PRODUCTO
# ======================================================================

SQL_PRODUCTO_DETALLE = text("""
SELECT
    p.codigo,
    p.nombre,
    p.descripcion,
    p.laboratorio,
    p.precio_venta,
    p.imagen_url,
    COALESCE(p.stock, 0) AS stock,
    COALESCE(p.categoria, '') AS categoria
FROM productos p
WHERE p.codigo = :codigo
LIMIT 1
""")

@router.get("/producto/{codigo}")
def detalle_producto(
    codigo: str,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),  # 🔒 exige login
):
    row = db.execute(SQL_PRODUCTO_DETALLE, {"codigo": codigo}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    r = dict(row)
    return {
        "codigo": r.get("codigo"),
        "nombre": r.get("nombre") or "",
        "descripcion": r.get("descripcion") or "",
        "laboratorio": r.get("laboratorio") or "",
        "categoria": r.get("categoria") or "",
        "precio_venta": float(r.get("precio_venta") or 0),
        "imagen_url": r.get("imagen_url") or None,
        "stock": int(r.get("stock") or 0),
    }

# ====== MARCAS ======
SQL_MARCAS = """
SELECT DISTINCT TRIM(p.laboratorio) AS marca
FROM productos p
WHERE TRIM(COALESCE(p.laboratorio,'')) <> ''
ORDER BY 1
"""

@router.get("/marcas")
def listar_marcas(
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),  # 🔒 exige login (quita si quieres público)
):
    rows = db.execute(text(SQL_MARCAS)).mappings().all()
    marcas = [r["marca"] for r in rows if r["marca"]]
    return {"marcas": marcas, "total": len(marcas)}

# ====== DESTACADOS ======
# Criterio simple: más stock y luego por nombre. Ajusta a ventas/fecha/etc si tienes esas columnas.
SQL_DESTACADOS = """
SELECT
    p.codigo,
    p.nombre,
    p.laboratorio,
    p.precio_venta,
    p.imagen_url,
    COALESCE(p.stock, 0) AS stock
FROM productos p
WHERE COALESCE(p.stock, 0) > 0
ORDER BY p.stock DESC, p.nombre ASC
LIMIT :limit
"""

@router.get("/destacados")
def destacados(
    limit: int = 12,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),  # 🔒 exige login (quita si quieres público)
):
    rows = db.execute(text(SQL_DESTACADOS), {"limit": limit}).mappings().all()
    items = [{
        "codigo": r["codigo"],
        "nombre": r["nombre"] or "",
        "laboratorio": r["laboratorio"] or "",
        "precio_venta": float(r["precio_venta"] or 0),
        "imagen_url": r["imagen_url"] or None,
        "stock": int(r["stock"] or 0),
    } for r in rows]
    return {"items": items, "total": len(items)}
