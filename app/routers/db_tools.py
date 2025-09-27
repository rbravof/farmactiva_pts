# app/routers/db_tools.py
from __future__ import annotations

import os
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Header, Query
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import SQLAlchemyError

# Usa el mismo SessionLocal que el resto de la app
from app.database import SessionLocal

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
    url = os.getenv("PTS_DB_URL") or os.getenv("DATABASE_URL") or _RENDER_URL
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url.split("postgresql://", 1)[1]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url

def _mask(url: str) -> str:
    try:
        p = urlparse(url)
        if "@" not in p.netloc or ":" not in p.netloc:
            return url
        creds, host = p.netloc.split("@", 1)
        user, _pwd = creds.split(":", 1)
        return p._replace(netloc=f"{user}:***@{host}").geturl()
    except Exception:
        return url

ENGINE = create_engine(_db_url(), pool_pre_ping=True, future=True)
router = APIRouter(prefix="/admin/db", tags=["DB"])

def _require_setup_token(hdr_token: str | None, q_token: str | None):
    """Permite inicializar sin login, pero exige un token de setup."""
    expected = os.getenv("SETUP_TOKEN", "").strip()
    provided = (hdr_token or q_token or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="Falta SETUP_TOKEN en el servidor.")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Token de setup inválido.")

@router.get("/url")
def db_url_info() -> Dict[str, Any]:
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
    try:
        with ENGINE.connect() as conn:
            version = conn.execute(text("select version()")).scalar_one()
            now = conn.execute(text("select now()")).scalar_one()
            current_db = conn.execute(text("select current_database()")).scalar_one()
            current_user = conn.execute(text("select current_user")).scalar_one()
        return {"ok": True, "version": version, "now": str(now),
                "database": current_db, "user": current_user}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"DB ping error: {e}")

@router.get("/tables")
def list_tables(schema: str = "public") -> Dict[str, Any]:
    try:
        insp = inspect(ENGINE)
        tables = insp.get_table_names(schema=schema)
        return {"ok": True, "schema": schema, "tables": tables}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"No se pudo listar tablas: {e}")

@router.post("/create-tables")
def admin_db_create_tables(
    x_setup_token: str | None = Header(None, convert_underscores=False),
    token: str | None = Query(None),
):
    """
    Crea todas las tablas declaradas en app.models.Base.metadata.
    Protegido por cabecera X-Setup-Token o query ?token=...
    """
    _require_setup_token(x_setup_token, token)

    db = SessionLocal()
    try:
        # Extensión CITEXT (si Render lo permite)
        try:
            db.execute(text("CREATE EXTENSION IF NOT EXISTS citext;"))
            db.commit()
        except Exception:
            db.rollback()
            print("[db-tools] WARN: no se pudo crear extensión citext (permiso o ya existe)")

        # Importa modelos para registrar todas las tablas en el mismo metadata
        from app import models as m
        # Forzamos tocar clases con FKs importantes
        _ = (m.Region, m.Comuna)

        # Ejecuta create_all en el bind de la sesión
        m.Base.metadata.create_all(bind=db.get_bind())
        return {"ok": True, "created": True}
    except Exception as e:
        db.rollback()
        print("[db-tools] ERROR create_all:", e)
        raise HTTPException(status_code=500, detail=f"Error creando tablas: {e}")
    finally:
        db.close()
