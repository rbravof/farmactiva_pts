# app/routers/tienda.py
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_, literal
from app.database import get_db
from app.models import Producto, ProductoSucursal, Laboratorio, CodigoBarra, Usuario
from app.auth import get_current_user_optional  # opcional (puede ser None)

router = APIRouter(prefix="/api/tienda", tags=["tienda"])

# ====== Schemas ======
class ProductoItem(BaseModel):
    codigo: str
    nombre: str
    laboratorio: str | None = None
    ean: str | None = None
    precio_venta: float | None = None
    stock: int | None = None
    imagen_url: str | None = None  # si aún no tienes imágenes reales, deja None

class SearchResponse(BaseModel):
    items: list[ProductoItem]
    total: int
    offset: int
    limit: int
    q: str | None = None

# ====== Endpoint ======
@router.get("/buscar", response_model=SearchResponse)
def buscar_productos_tienda(
    q: str | None = Query(None, min_length=2, description="Texto de búsqueda por nombre o EAN"),
    offset: int = Query(0, ge=0),
    limit: int = Query(12, ge=1, le=60),
    solo_con_stock: bool = Query(True),
    db: Session = Depends(get_db),
    usuario: Usuario | None = Depends(get_current_user_optional),
):
    # 📡 Trazas
    print("📡 [/api/tienda/buscar] q=", repr(q), " offset=", offset, " limit=", limit,
          " solo_con_stock=", solo_con_stock,
          " usuario=", getattr(usuario, "usuario", None),
          " id_farmacia=", getattr(usuario, "id_farmacia", None))

    # 1) Determinar id_farmacia objetivo
    #    - Si hay usuario autenticado, usamos su farmacia
    #    - Si no, para MVP forzamos id_farmacia=1 (ajústalo al dominio/subdominio en el futuro)
    id_farmacia = getattr(usuario, "id_farmacia", None) or 1

    # 2) Base Query: joins y filtros mínimos para la tienda
    #    - Producto.activo = True
    #    - ProductoSucursal.visible = True
    #    - ProductoSucursal.id_farmacia = id_farmacia
    #    - stock > 0 (si solo_con_stock=True)
    ps = db.query(
        Producto.codigo.label("codigo"),
        Producto.nombre.label("nombre"),
        Laboratorio.nombre.label("laboratorio"),
        # Tomamos el primer EAN si existe (puede ajustarse a es_principal=True si lo tienes)
        func.coalesce(
            db.query(CodigoBarra.ean)
              .filter(CodigoBarra.codigo == Producto.codigo)
              .limit(1)  # simple y rápido
              .correlate(Producto)
              .as_scalar(),
            literal(None)
        ).label("ean"),
        ProductoSucursal.precio_venta.label("precio_venta"),
        ProductoSucursal.stock.label("stock"),
        literal(None).label("imagen_url"),  # Placeholder si aún no tienes media
    ).join(
        ProductoSucursal, and_(
            ProductoSucursal.codigo == Producto.codigo,
            ProductoSucursal.id_farmacia == id_farmacia,
            ProductoSucursal.visible == True
        )
    ).outerjoin(
        Laboratorio, Laboratorio.id == Producto.laboratorio_id
    ).filter(
        Producto.activo == True
    )

    if solo_con_stock:
        ps = ps.filter(ProductoSucursal.stock > 0)

    # 3) Búsqueda por texto (nombre) o EAN
    if q:
        s = f"%{q}%"
        ps = ps.filter(
            or_(
                Producto.nombre.ilike(s),
                Producto.nombre.ilike(s.replace(" ", "%")),  # tolerante a espacios
                # Búsqueda por EAN: existe algún código de barra que contenga q
                Producto.codigo.in_(
                    db.query(CodigoBarra.codigo).filter(CodigoBarra.ean.ilike(s))
                )
            )
        )

    # 4) Total antes de paginar
    total = ps.distinct(Producto.codigo).count()

    # 5) Ordenamiento simple:
    #    - Si hay q, prioriza "empieza con"
    if q:
        ps = ps.order_by(
            func.case(
                (Producto.nombre.ilike(f"{q}%"), 0),
                else_=1
            ),
            func.length(Producto.nombre),
            Producto.nombre.asc()
        )
    else:
        # Sin q, orden por nombre
        ps = ps.order_by(Producto.nombre.asc())

    # 6) Paginación
    filas = ps.offset(offset).limit(limit).all()

    # 7) Serializar
    items = [
        ProductoItem(
            codigo=f.codigo,
            nombre=f.nombre,
            laboratorio=f.laboratorio,
            ean=f.ean,
            precio_venta=float(f.precio_venta) if f.precio_venta is not None else None,
            stock=int(f.stock) if f.stock is not None else None,
            imagen_url=f.imagen_url
        ) for f in filas
    ]

    print("✅ [/api/tienda/buscar] devueltos=", len(items), " de total=", total)
    return SearchResponse(items=items, total=total, offset=offset, limit=limit, q=q or None)
