# app/scripts/check_schema.py
from __future__ import annotations
import os, sys, argparse
from pathlib import Path
from typing import Set, List, Dict
from sqlalchemy import create_engine, inspect
from sqlalchemy.schema import CreateTable
from sqlalchemy.engine.reflection import Inspector

# --- Resolver raíz del proyecto para que "import app.models" funcione ---
THIS = Path(__file__).resolve()
CANDIDATES = [
    THIS.parents[2],  # repo_root si el script está en app/scripts
    THIS.parents[1],  # por si lo moviste a scripts/ en raíz
    Path.cwd(),       # cwd (último recurso)
]
for cand in CANDIDATES:
    if (cand / "app" / "models.py").exists():
        sys.path.insert(0, str(cand))
        break

# ========= Configuración de tolerancias =========
# Tablas que SÍ existen en la DB pero NO quieres auditar (catálogos/legacy/auxiliares)
WHITELIST_TABLES: Set[str] = {
    "public.comunas",
    "public.regiones",
    "public.listas_precios",
    "public.pedido_estado_historial",
    "public.politicas_precio",
    "public.precios",
    "public.productos_categorias",
    "public.regla_ambitos",
    "public.regla_segmentos",
    "public.reglas_precio",
    "public.segmento_membresias",
    "public.segmentos_cliente",
    "public.v_cat_id",
    "public.web_menu_items",
}

# Columnas “extra” permitidas por tabla (presentes en DB, no en models.py, y NO queremos que alarme)
ALLOWED_EXTRA_COLUMNS: Dict[str, Set[str]] = {
    "public.clientes_direcciones": {"fecha_actualizacion", "fecha_creacion", "lat", "lon"},
    "public.envio_tarifas": {"dist_max_km", "dist_min_km", "id_bodega"},
    "public.pedido_notas": {"autor_nombre", "autor_rol"},
    "public.pedidos": {
        "descuento_total", "descuentos", "id_bodega", "id_tarifa_aplicada",
        "mp_init_point", "mp_preference_id", "pago_estado", "pago_moneda",
        "pago_monto", "pago_proveedor", "subtotal_items", "total_bruto",
    },
    "public.tipo_medicamento": {"activo", "creado_en"},
}

# Activar modo estricto (ignora whitelists) si SCHEMA_AUDIT_STRICT=1|true|yes
SCHEMA_AUDIT_STRICT = os.getenv("SCHEMA_AUDIT_STRICT", "0").lower() in ("1", "true", "yes")


def _norm_table(table_name: str, schema: str) -> str:
    """Devuelve 'schema.table'. Si ya viene calificado, lo deja igual."""
    if "." in table_name:
        return table_name
    return f"{schema}.{table_name}"


def _split_qualified(qname: str) -> tuple[str, str]:
    """'schema.table' -> (schema, table)"""
    if "." not in qname:
        return "public", qname
    s, t = qname.split(".", 1)
    return s, t


def _is_whitelisted_table(qname: str) -> bool:
    return qname in WHITELIST_TABLES


def _get_db_url() -> str:
    for key in ("DATABASE_URL", "SQLALCHEMY_DATABASE_URL"):
        v = os.getenv(key)
        if v:
            return v
    print("[schema-audit] ERROR: define DATABASE_URL o SQLALCHEMY_DATABASE_URL en el entorno.", file=sys.stderr)
    sys.exit(2)


def _load_models():
    try:
        from app.models import Base
    except Exception as e:
        print(f"[schema-audit] ERROR importando app.models.Base: {e}", file=sys.stderr)
        # Tips
        print("Tips: 1) ejecuta como módulo desde la raíz:  python -m app.scripts.check_schema")
        print("      2) crea app/__init__.py y app/scripts/__init__.py (vacíos)")
        print("      3) o exporta PYTHONPATH a la raíz del repo")
        sys.exit(2)
    return Base


def _compile_create_sql(table, engine) -> str:
    try:
        return str(CreateTable(table).compile(dialect=engine.dialect)).rstrip() + ";"
    except Exception:
        return "-- (no se pudo compilar CREATE TABLE)"


def main():
    parser = argparse.ArgumentParser(description="Audita que la DB coincida con app.models.")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--allow-extra", default="alembic_version",
                        help="Tablas extra permitidas, separadas por coma (default: alembic_version)")
    parser.add_argument("--show-create", action="store_true",
                        help="Muestra CREATE TABLE sugerido para tablas faltantes")
    args = parser.parse_args()

    allow_extra_cli = set([t.strip() for t in (args.allow_extra or "").split(",") if t.strip()])

    Base = _load_models()

    # Normaliza nombres de tablas declaradas en models -> 'schema.table'
    declared_raw: Set[str] = set(Base.metadata.tables.keys())
    declared_q: Set[str] = set()
    model_key_by_qname: Dict[str, str] = {}
    for key in declared_raw:
        # key puede venir 'tabla' o 'schema.tabla'
        qname = key if "." in key else f"{args.schema}.{key}"
        declared_q.add(qname)
        model_key_by_qname[qname] = key  # para buscar el Table en Base.metadata.tables

    print(f"[schema-audit] Declaradas en models.py: {len(declared_q)}")

    db_url = _get_db_url()
    engine = create_engine(db_url, future=True)
    insp: Inspector = inspect(engine)

    db_tables_all: List[str] = insp.get_table_names(schema=args.schema)  # nombres sin schema
    # Filtra internas de PG y allow-extra CLI
    def _ignored(name: str) -> bool:
        if name in allow_extra_cli:
            return True
        if name.startswith("pg_") or name.startswith("sqlalchemy_"):
            return True
        return False

    db_tables_filtered = {t for t in db_tables_all if not _ignored(t)}
    db_tables_q: Set[str] = {f"{args.schema}.{t}" for t in db_tables_filtered}
    print(f"[schema-audit] Tablas en DB ({args.schema}): {len(db_tables_all)} (útiles: {len(db_tables_q)})")

    # --- Comparación de tablas ---
    missing_in_db = declared_q - db_tables_q
    # Extra = en DB pero no en modelos, con whitelist si no es estricto
    extra_in_db = sorted(
        t for t in (db_tables_q - declared_q)
        if SCHEMA_AUDIT_STRICT or not _is_whitelisted_table(t)
    )

    ok = True
    if missing_in_db:
        ok = False
        print("\n### ❌ Tablas DECLARADAS pero FALTAN en DB:")
        for t in sorted(missing_in_db):
            print(f"  - {t}")
            if args.show_create:
                model_key = model_key_by_qname.get(t, t.split(".", 1)[-1])
                if model_key in Base.metadata.tables:
                    tbl = Base.metadata.tables[model_key]
                    sql = _compile_create_sql(tbl, engine).replace("\n", "\n    ")
                    print("    -- SQL sugerido:")
                    print(f"    {sql}")
                else:
                    print("    -- SQL sugerido:")
                    print("    -- (no se pudo ubicar la tabla en Base.metadata)")

    if extra_in_db:
        ok = False
        print("\n### ❌ Tablas EXTRA en DB (no están en models.py):")
        for t in extra_in_db:
            print(f"  - {t}")
        print("    -- Sugerencia para limpiar (REVISA antes de ejecutar):")
        for t in extra_in_db:
            print(f"    DROP TABLE IF EXISTS {t} CASCADE;")

    # --- Columnas ---
    print("\n### Revisión de columnas:")
    # Intersección por nombres normalizados
    inter = declared_q & db_tables_q
    for qname in sorted(inter):
        model_key = model_key_by_qname.get(qname, qname.split(".", 1)[-1])
        if model_key not in Base.metadata.tables:
            continue
        table_obj = Base.metadata.tables[model_key]

        schema_name, table_name = _split_qualified(qname)
        db_cols = set(col["name"] for col in insp.get_columns(table_name, schema=schema_name))
        model_cols = set(table_obj.columns.keys())

        # Faltantes (siempre alertar)
        miss_cols = model_cols - db_cols

        # Extras (filtrar por ALLOWED_EXTRA_COLUMNS, salvo modo estricto)
        allowed_extras = ALLOWED_EXTRA_COLUMNS.get(qname, set())
        extra_cols = set()
        for c in db_cols - model_cols:
            if SCHEMA_AUDIT_STRICT:
                extra_cols.add(c)
            else:
                if c not in allowed_extras:
                    extra_cols.add(c)

        if miss_cols or extra_cols:
            ok = False
            print(f"  • {qname}")
            if miss_cols:
                print(f"    - Faltan en DB: {sorted(miss_cols)}")
            if extra_cols:
                print(f"    - Sobran en DB: {sorted(extra_cols)}")

    # --- FKs con destino inexistente ---
    print("\n### Revisión de Foreign Keys rotas:")
    for qname in sorted(inter):
        schema_name, table_name = _split_qualified(qname)
        fks = insp.get_foreign_keys(table_name, schema=schema_name) or []
        broken = []
        for fk in fks:
            ref_table = fk.get("referred_table")
            ref_schema = fk.get("referred_schema") or schema_name
            ref_qname = f"{ref_schema}.{ref_table}" if ref_table else None
            if ref_qname and (ref_qname not in db_tables_q):
                # Si no es estricto y está whitelisteada, no alertes
                if not SCHEMA_AUDIT_STRICT and _is_whitelisted_table(ref_qname):
                    continue
                broken.append((fk.get("name"), ref_qname))
        if broken:
            ok = False
            print(f"  • {qname} -> FKs con destino inexistente: {broken}")

    print("\n### Resultado:")
    if ok:
        print("✅ Esquema consistente con models.py (sin diferencias relevantes).")
        sys.exit(0)
    else:
        print("❗ Hay diferencias. Corrige antes de desplegar a QA.")
        sys.exit(1)


if __name__ == "__main__":
    main()
