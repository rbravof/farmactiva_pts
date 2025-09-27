from __future__ import annotations

import os
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import SQLAlchemyError

# ------------------------------------------------------------
# Conexion DB (Render exige SSL). Usa PTS_DB_URL/DATABASE_URL
# o el fallback embebido.
# ------------------------------------------------------------
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

# -------------------- Utils token setup ---------------------
def _header_token(request: Request) -> str:
    return (request.headers.get("X-Setup-Token")
            or request.headers.get("x-setup-token")
            or "").strip()

def _env_token() -> str:
    return (os.getenv("SETUP_TOKEN") or "").strip()

def _assert_setup_token(request: Request):
    env_tok = _env_token()
    hdr_tok = _header_token(request)
    if not env_tok:
        raise HTTPException(status_code=400,
                            detail="SETUP_TOKEN no está configurado en el entorno.")
    if hdr_tok != env_tok:
        # trazas sin exponer el token
        print("[db-tools] Setup token mismatch "
              f"(len hdr={len(hdr_tok)}, len env={len(env_tok)})")
        raise HTTPException(status_code=403, detail="Token de setup inválido.")

# -------------------- Info & ping ----------------------------
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
        return {"ok": True, "schema": schema,
                "tables": insp.get_table_names(schema=schema)}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"No se pudo listar tablas: {e}")

@router.get("/setup-status")
def setup_status(request: Request):
    env_tok = _env_token()
    hdr_tok = _header_token(request)
    return {"configured": bool(env_tok),
            "header_present": bool(hdr_tok),
            "match": (env_tok and hdr_tok and env_tok == hdr_tok)}

# -------------------- CREATE TABLES --------------------------
@router.post("/create-tables")   # <- SIN /admin/db extra
def create_tables(request: Request):
    # Validación simple por token de setup
    _assert_setup_token(request)

    # 1) Extensión citext
    try:
        with ENGINE.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext;"))
    except Exception as e:
        print("[db-tools] WARN citext:", e)

    # 2) Registrar modelos y crear tablas
    try:
        # IMPORTA TODOS los modelos para que se registren en Base.metadata
        from app import models as m  # noqa: F401

        # Crear todo
        m.Base.metadata.create_all(bind=ENGINE)

        # Devolver listado
        insp = inspect(ENGINE)
        tables = insp.get_table_names(schema="public")
        return {"ok": True, "created": True, "tables": tables}
    except Exception as e:
        print("[db-tools] ERROR create_all:", e)
        raise HTTPException(status_code=500, detail=f"Error creando tablas: {e}")
