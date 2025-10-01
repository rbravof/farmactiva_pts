# app/routers/carrier_portal.py
from __future__ import annotations
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, Form, HTTPException, UploadFile, File
from starlette.responses import RedirectResponse
from starlette import status
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.routers.admin_security import require_transportista
from app.models import Usuario
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/carrier", tags=["Carrier"])

# -------------------------
# Helpers: introspección DB
# -------------------------
def _has_table(db: Session, table: str) -> bool:
    row = db.execute(
        text("""
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema='public' AND table_name=:t
        """),
        {"t": table}
    ).first()
    return bool(row)

def _has_column(db: Session, table: str, column: str) -> bool:
    row = db.execute(
        text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t AND column_name=:c
        """),
        {"t": table, "c": column}
    ).first()
    return bool(row)

# -------------------------
# Query builder (soporta 2 esquemas):
#   A) Tabla envios (envios.transportista_id)
#   B) En pedidos (pedidos.transportista_id o pedidos.transportista_usuario)
# -------------------------
def _q_list_envios(db: Session, user: Usuario) -> List[Dict[str, Any]]:
    """
    Devuelve filas con: id, numero, estado, direccion, comuna, fecha, etc.
    Ajusta según tus campos reales. Se detecta la mejor variante.
    """
    # Variante A: existe tabla 'envios'
    if _has_table(db, "envios"):
        # ¿tiene columna transportista_id?
        if _has_column(db, "envios", "transportista_id"):
            sql = text("""
                SELECT
                  e.id_envio         AS id,
                  COALESCE(p.numero, p.id_pedido::text) AS numero,
                  e.estado           AS estado,
                  COALESCE(CONCAT_WS(', ', de.calle, de.numero, de.comuna), de.texto) AS direccion,
                  COALESCE(de.comuna, '') AS comuna,
                  e.creado_en        AS creado_en
                FROM envios e
                LEFT JOIN pedidos p ON p.id_pedido = e.id_pedido
                LEFT JOIN direccion_envio de ON de.id_direccion = p.direccion_envio_id
                WHERE e.transportista_id IN (
                  SELECT id_transportista FROM transportistas WHERE usuario = :usuario
                )
                ORDER BY e.creado_en DESC
                LIMIT 200
            """)
            rows = db.execute(sql, {"usuario": user.usuario}).mappings().all()
            return [dict(r) for r in rows]

    # Variante B: vivir en pedidos
    # Prioridad: transportista_id -> tabla transportistas
    if _has_table(db, "pedidos") and _has_table(db, "transportistas") and _has_column(db, "pedidos", "transportista_id"):
        sql = text("""
            SELECT
              p.id_pedido         AS id,
              COALESCE(p.numero, p.id_pedido::text) AS numero,
              p.estado_codigo     AS estado,
              COALESCE(CONCAT_WS(', ', de.calle, de.numero, de.comuna), de.texto) AS direccion,
              COALESCE(de.comuna, '') AS comuna,
              p.creado_en         AS creado_en
            FROM pedidos p
            LEFT JOIN direccion_envio de ON de.id_direccion = p.direccion_envio_id
            WHERE p.transportista_id IN (
              SELECT id_transportista FROM transportistas WHERE usuario = :usuario
            )
            ORDER BY p.creado_en DESC
            LIMIT 200
        """)
        rows = db.execute(sql, {"usuario": user.usuario}).mappings().all()
        return [dict(r) for r in rows]

    # Alternativa: columna transportista_usuario en pedidos
    if _has_table(db, "pedidos") and _has_column(db, "pedidos", "transportista_usuario"):
        sql = text("""
            SELECT
              p.id_pedido         AS id,
              COALESCE(p.numero, p.id_pedido::text) AS numero,
              p.estado_codigo     AS estado,
              COALESCE(CONCAT_WS(', ', de.calle, de.numero, de.comuna), de.texto) AS direccion,
              COALESCE(de.comuna, '') AS comuna,
              p.creado_en         AS creado_en
            FROM pedidos p
            LEFT JOIN direccion_envio de ON de.id_direccion = p.direccion_envio_id
            WHERE lower(p.transportista_usuario) = lower(:usuario)
            ORDER BY p.creado_en DESC
            LIMIT 200
        """)
        rows = db.execute(sql, {"usuario": user.usuario}).mappings().all()
        return [dict(r) for r in rows]

    # Si nada encaja, devolvemos vacío
    return []

def _q_detalle_envio(db: Session, user: Usuario, envio_id: int) -> Dict[str, Any] | None:
    # Variante A: tabla envios
    if _has_table(db, "envios"):
        if _has_column(db, "envios", "transportista_id"):
            sql = text("""
                SELECT
                  e.id_envio         AS id,
                  COALESCE(p.numero, p.id_pedido::text) AS numero,
                  e.estado           AS estado,
                  COALESCE(CONCAT_WS(', ', de.calle, de.numero, de.comuna), de.texto) AS direccion,
                  COALESCE(de.comuna, '') AS comuna,
                  p.nombre_cliente   AS cliente,    -- TODO: ajusta a tus campos reales
                  p.telefono_cliente AS telefono,   -- TODO
                  p.email_cliente    AS email,      -- TODO
                  e.creado_en        AS creado_en
                FROM envios e
                LEFT JOIN pedidos p ON p.id_pedido = e.id_pedido
                LEFT JOIN direccion_envio de ON de.id_direccion = p.direccion_envio_id
                WHERE e.id_envio = :id
                  AND e.transportista_id IN (
                    SELECT id_transportista FROM transportistas WHERE usuario = :usuario
                  )
                LIMIT 1
            """)
            row = db.execute(sql, {"id": envio_id, "usuario": user.usuario}).mappings().first()
            return dict(row) if row else None

    # Variante B1: pedidos.transportista_id
    if _has_table(db, "pedidos") and _has_table(db, "transportistas") and _has_column(db, "pedidos", "transportista_id"):
        sql = text("""
            SELECT
              p.id_pedido         AS id,
              COALESCE(p.numero, p.id_pedido::text) AS numero,
              p.estado_codigo     AS estado,
              COALESCE(CONCAT_WS(', ', de.calle, de.numero, de.comuna), de.texto) AS direccion,
              COALESCE(de.comuna, '') AS comuna,
              p.nombre_cliente    AS cliente,      -- TODO: ajusta
              p.telefono_cliente  AS telefono,     -- TODO
              p.email_cliente     AS email,        -- TODO
              p.creado_en         AS creado_en
            FROM pedidos p
            LEFT JOIN direccion_envio de ON de.id_direccion = p.direccion_envio_id
            WHERE p.id_pedido = :id
              AND p.transportista_id IN (
                SELECT id_transportista FROM transportistas WHERE usuario = :usuario
              )
            LIMIT 1
        """)
        row = db.execute(sql, {"id": envio_id, "usuario": user.usuario}).mappings().first()
        return dict(row) if row else None

    # Variante B2: pedidos.transportista_usuario
    if _has_table(db, "pedidos") and _has_column(db, "pedidos", "transportista_usuario"):
        sql = text("""
            SELECT
              p.id_pedido         AS id,
              COALESCE(p.numero, p.id_pedido::text) AS numero,
              p.estado_codigo     AS estado,
              COALESCE(CONCAT_WS(', ', de.calle, de.numero, de.comuna), de.texto) AS direccion,
              COALESCE(de.comuna, '') AS comuna,
              p.nombre_cliente    AS cliente,      -- TODO
              p.telefono_cliente  AS telefono,     -- TODO
              p.email_cliente     AS email,        -- TODO
              p.creado_en         AS creado_en
            FROM pedidos p
            LEFT JOIN direccion_envio de ON de.id_direccion = p.direccion_envio_id
            WHERE p.id_pedido = :id
              AND lower(p.transportista_usuario) = lower(:usuario)
            LIMIT 1
        """)
        row = db.execute(sql, {"id": envio_id, "usuario": user.usuario}).mappings().first()
        return dict(row) if row else None

    return None

# -------------------------
# Rutas
# -------------------------
@router.get("")
def carrier_home(request: Request, user: Usuario = Depends(require_transportista), db: Session = Depends(get_db)):
    # Muestra un dashboard muy simple
    envios = _q_list_envios(db, user)
    # Podrías agrupar por estado aquí para contadores
    return templates.TemplateResponse(
        "carrier_home.html",
        {"request": request, "user": user, "envios": envios[:8]},  # últimos 8
    )

@router.get("/envios")
def carrier_envios_list(request: Request, user: Usuario = Depends(require_transportista), db: Session = Depends(get_db)):
    envios = _q_list_envios(db, user)
    return templates.TemplateResponse(
        "carrier_envios_list.html",
        {"request": request, "user": user, "envios": envios},
    )

@router.get("/envios/{envio_id:int}")
def carrier_envio_detalle(request: Request, envio_id: int, user: Usuario = Depends(require_transportista), db: Session = Depends(get_db)):
    envio = _q_detalle_envio(db, user, envio_id)
    if not envio:
        raise HTTPException(status_code=404, detail="Envío no encontrado o no asignado a ti")
    return templates.TemplateResponse(
        "carrier_envio_detalle.html",
        {"request": request, "user": user, "envio": envio},
    )

# Cambiar estado (transición controlada) - aquí solo esqueleto
@router.post("/envios/{envio_id:int}/estado")
def carrier_envio_cambiar_estado(
    request: Request,
    envio_id: int,
    nuevo_estado: str = Form(...),
    user: Usuario = Depends(require_transportista),
    db: Session = Depends(get_db),
):
    # TODO: valida transición contra tu tabla pedido_estado_transiciones / envío_estados
    # Inserta evento y actualiza estado. Por ahora, placeholder no destructivo:
    # db.execute(text("UPDATE ... WHERE id=... AND ..."), {...}); db.commit()
    return RedirectResponse(url=f"/carrier/envios/{envio_id}", status_code=status.HTTP_303_SEE_OTHER)

# Subir POD (foto/firma) - esqueleto
@router.post("/envios/{envio_id:int}/pod")
def carrier_envio_pod(
    request: Request,
    envio_id: int,
    evidencia: UploadFile = File(...),
    user: Usuario = Depends(require_transportista),
    db: Session = Depends(get_db),
):
    # TODO: guarda archivo (disco/S3) y registra evento. Placeholder:
    # filename = f"pod_{envio_id}_{int(time.time())}.jpg"
    # with open(f"media/{filename}", "wb") as f: f.write(await evidencia.read())
    return RedirectResponse(url=f"/carrier/envios/{envio_id}", status_code=status.HTTP_303_SEE_OTHER)
