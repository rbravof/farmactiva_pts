# app/routers/db_tools.py
from __future__ import annotations

import os
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import SQLAlchemyError

# -------------------------------------------------------------------
# URL de Render con todos los datos (fallback si no hay variables)
# -------------------------------------------------------------------
_RENDER_URL = (
    "postgresql+psycopg2://"
    "farmactiva_qa_db_user:DRPbSgZXq91VitevSYRZtrShyEizv6me"
    "@dpg-d3bfu13ipnbc73fr7j8g-a.oregon-postgres.render.com:5432"
    "/farmactiva_qa_db?sslmode=require"
)

def _db_url() -> str:
    """Toma PTS_DB_URL/DATABASE_URL o usa el fallback de Render; normaliza driver y SSL."""
    url = os.getenv("PTS_DB_URL") or os.getenv("DATABASE_URL") or _RENDER_URL

    # Asegura driver psycopg2 para SQLAlchemy si viene como postgresql://
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url.split("postgresql://", 1)[1]

    # Asegura sslmode=require (Render lo exige)
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"

    return url

def _mask(url: str) -> str:
    """Oculta la contraseña en la URL para devolverla por API."""
    try:
        parsed = urlparse(url)
        if "@" not in parsed.netloc or ":" not in parsed.netloc:
            return url
        creds, host = parsed.netloc.split("@", 1)
        user, _pwd = creds.split(":", 1)
        netloc_mask = f"{user}:***@{host}"
        return parsed._replace(netloc=netloc_mask).geturl()
    except Exception:
        return url

# Engine global y router
ENGINE = create_engine(_db_url(), pool_pre_ping=True, future=True)
router = APIRouter(prefix="/admin/db", tags=["DB"])

@router.get("/url")
def db_url_info() -> Dict[str, Any]:
    """Devuelve la URL (sin password) y sus partes para verificar conexión."""
    url = _db_url()
    p = urlparse(url)
    q = parse_qs(p.query or "")
    return {
        "database_url_masked": _mask(url),
        "driver": p.scheme,
        "user": (p.username or ""),
        "host": (p.hostname or ""),
        "port": p.port or 5432,
        "database": p.path.lstrip("/"),
        "sslmode": (q.get("sslmode", ["require"])[0]),
        "from_env": bool(os.getenv("PTS_DB_URL") or os.getenv("DATABASE_URL")),
    }

@router.get("/ping")
def db_ping() -> Dict[str, Any]:
    """Hace un SELECT simple para comprobar que la DB responde."""
    try:
        with ENGINE.connect() as conn:
            version = conn.execute(text("select version()")).scalar_one()
            now = conn.execute(text("select now()")).scalar_one()
            current_db = conn.execute(text("select current_database()")).scalar_one()
            current_user = conn.execute(text("select current_user")).scalar_one()
        return {
            "ok": True,
            "version": version,
            "now": str(now),
            "database": current_db,
            "user": current_user,
        }
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"DB ping error: {e}")

@router.get("/tables")
def list_tables(schema: str = "public") -> Dict[str, Any]:
    """Lista tablas del esquema (public por defecto)."""
    try:
        insp = inspect(ENGINE)
        tables = insp.get_table_names(schema=schema)
        return {"ok": True, "schema": schema, "tables": tables}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"No se pudo listar tablas: {e}")

@router.post("/create-tables")
def create_tables() -> Dict[str, Any]:
    """
    Crea todas las tablas definidas en app.models.Base en esta DB.
    Úsalo una sola vez en bases vacías.
    """
    try:
        from app.models import Base  # importa tus modelos
        Base.metadata.create_all(bind=ENGINE)
        return {"ok": True, "msg": "Tablas creadas (si no existían)."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando tablas: {e}")
