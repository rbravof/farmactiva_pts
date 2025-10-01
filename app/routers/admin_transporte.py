# file: app/routers/admin_transporte.py
from fastapi import APIRouter, Depends, Request, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional, List
from datetime import date, datetime
from io import StringIO
from app.database import get_db
from app.routers.admin_security import require_staff, require_admin
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/admin/transporte", tags=["Admin Transporte"])

# ================================
# SQL helpers
# ================================
SQL_KPIS_7D = text("SELECT * FROM public.v_transporte_kpis_7d")

SQL_LIST_ASIGNADOS = text("""
SELECT p.id_pedido, p.numero,
       COALESCE(est.nombre, p.estado_codigo) AS estado,
       v.transportista_nombre, v.estado_logistico, v.asignado_en
FROM public.pedidos p
JOIN public.v_pedido_transportista_vigente v ON v.id_pedido = p.id_pedido
LEFT JOIN public.pedido_estados est ON est.codigo = p.estado_codigo
WHERE v.estado_logistico IN ('ASIGNADO','RETIRADO','EN_TRANSITO')
ORDER BY v.asignado_en DESC
LIMIT 300
""")

SQL_LIST_ENTREGADOS = text("""
SELECT p.id_pedido, p.numero,
       COALESCE(est.nombre, p.estado_codigo) AS estado,
       MAX(ev.creado_en) AS entregado_en
FROM public.pedidos p
JOIN public.pedido_envio_eventos ev ON ev.id_pedido = p.id_pedido AND ev.estado='ENTREGADO'
LEFT JOIN public.pedido_estados est ON est.codigo = p.estado_codigo
GROUP BY p.id_pedido, p.numero, est.nombre, p.estado_codigo
ORDER BY entregado_en DESC
LIMIT 300
""")

SQL_LIST_INCIDENCIAS = text("""
SELECT i.id_incidencia, i.id_asignacion, p.id_pedido, p.numero,
       i.tipo, i.descripcion, i.foto_url, i.creado_en
FROM public.incidencias i
JOIN public.pedido_asignaciones a ON a.id_asignacion = i.id_asignacion
JOIN public.pedidos p ON p.id_pedido = a.id_pedido
ORDER BY i.creado_en DESC
LIMIT 300
""")

SQL_PEDIDOS_FILTRO = text("""
SELECT p.id_pedido, p.numero,
       COALESCE(est.nombre, p.estado_codigo) AS estado,
       v.transportista_nombre, v.estado_logistico, v.asignado_en,
       c.nombre AS cliente_nombre
FROM public.pedidos p
LEFT JOIN public.pedido_estados est ON est.codigo = p.estado_codigo
LEFT JOIN public.clientes c ON c.id_cliente = p.id_cliente
LEFT JOIN public.v_pedido_transportista_vigente v ON v.id_pedido = p.id_pedido
WHERE (:estado IS NULL OR v.estado_logistico = :estado)
  AND (:transportista IS NULL OR v.transportista_nombre ILIKE '%'||:transportista||'%')
  AND (:cliente IS NULL OR c.nombre ILIKE '%'||:cliente||'%')
  AND (:desde::date IS NULL OR p.creado_en::date >= :desde::date)
  AND (:hasta::date IS NULL OR p.creado_en::date <= :hasta::date)
ORDER BY p.id_pedido DESC
LIMIT 1000
""")

SQL_RUTAS_LIST = text("""
SELECT r.id_ruta, r.fecha, r.zona, r.estado,
       t.nombre AS transportista_nombre, r.capacidad_max, r.creado_en
FROM public.rutas r
LEFT JOIN public.transportistas t ON t.id_transportista = r.id_transportista
WHERE (:fecha IS NULL OR r.fecha = :fecha::date)
  AND (:estado IS NULL OR r.estado = :estado)
ORDER BY r.fecha DESC, r.id_ruta DESC
LIMIT 400
""")

SQL_RUTA_DETALLE = text("""
SELECT rd.id_ruta_det, rd.id_ruta, rd.id_pedido, rd.orden,
       p.numero, COALESCE(e.nombre, p.estado_codigo) AS estado,
       v.transportista_nombre
FROM public.rutas_detalle rd
JOIN public.pedidos p ON p.id_pedido = rd.id_pedido
LEFT JOIN public.pedido_estados e ON e.codigo = p.estado_codigo
LEFT JOIN public.v_pedido_transportista_vigente v ON v.id_pedido = p.id_pedido
WHERE rd.id_ruta = :id_ruta
ORDER BY COALESCE(rd.orden, 999999), rd.id_ruta_det
""")

SQL_RUTA_INSERT = text("""
INSERT INTO public.rutas (fecha, id_transportista, id_sucursal, zona, capacidad_max, estado, creado_en)
VALUES (:fecha, :id_transportista, :id_sucursal, NULLIF(:zona,''), NULLIF(:capacidad_max, NULL), 'PLANIFICADA', now())
RETURNING id_ruta
""")

SQL_RUTA_SET_ESTADO = text("""
UPDATE public.rutas SET estado = :estado WHERE id_ruta = :id_ruta
""")

SQL_RUTADET_ADD = text("""
INSERT INTO public.rutas_detalle (id_ruta, id_pedido, orden, creado_en)
SELECT :id_ruta, :id_pedido,
       (SELECT COALESCE(MAX(orden),0)+1 FROM public.rutas_detalle WHERE id_ruta=:id_ruta),
       now()
ON CONFLICT DO NOTHING
""")

SQL_RUTADET_DEL = text("""
DELETE FROM public.rutas_detalle WHERE id_ruta = :id_ruta AND id_pedido = :id_pedido
""")

SQL_TRANSPORTISTAS_ACTIVOS = text("""
SELECT id_transportista, nombre FROM public.transportistas
WHERE activo = TRUE
ORDER BY nombre
""")

SQL_EXPORT_ENTREGAS = text("""
SELECT p.id_pedido, p.numero, c.nombre AS cliente, d.comuna, d.ciudad, d.region,
       MAX(ev.creado_en) AS entregado_en, v.transportista_nombre
FROM public.pedidos p
JOIN public.pedido_envio_eventos ev ON ev.id_pedido = p.id_pedido AND ev.estado='ENTREGADO'
LEFT JOIN public.v_pedido_transportista_vigente v ON v.id_pedido = p.id_pedido
LEFT JOIN public.clientes c ON c.id_cliente = p.id_cliente
LEFT JOIN public.direcciones_envio d ON d.id_direccion = p.id_direccion_envio
WHERE ev.creado_en::date BETWEEN :desde::date AND :hasta::date
GROUP BY p.id_pedido, p.numero, c.nombre, d.comuna, d.ciudad, d.region, v.transportista_nombre
ORDER BY entregado_en DESC
""")

# ================================
# Dashboard (HTML)
# ================================
@router.get("", response_class=HTMLResponse)
def admin_transporte_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    staff: dict = Depends(require_staff),
):
    print("🚀 [TRANS] Dashboard - inicio")
    kpis = {}
    asignados, entregados, incidencias = [], [], []
    try:
        kpis = db.execute(SQL_KPIS_7D).mappings().first() or {}
        asignados = db.execute(SQL_LIST_ASIGNADOS).mappings().all()
        entregados = db.execute(SQL_LIST_ENTREGADOS).mappings().all()
        incidencias = db.execute(SQL_LIST_INCIDENCIAS).mappings().all()
        print(f"📡 [TRANS] KPIs cargados, asignados={len(asignados)}, entregados={len(entregados)}, incidencias={len(incidencias)}")
    except Exception as e:
        print(f"💥 [TRANS] error dashboard: {e}")

    return templates.TemplateResponse("admin_transporte_dashboard.html", {
        "request": request,
        "user": staff,
        "kpis": kpis,
        "asignados": asignados,
        "entregados": entregados,
        "incidencias": incidencias
    })

# ================================
# APIs JSON (filtros)
# ================================
@router.get("/api/kpis", response_model=dict)
def api_kpis(
    db: Session = Depends(get_db),
    _: dict = Depends(require_staff),
):
    data = db.execute(SQL_KPIS_7D).mappings().first() or {}
    print("📡 [TRANS] api_kpis")
    return data

@router.get("/api/pedidos", response_model=list)
def api_pedidos(
    estado: Optional[str] = Query(default=None, description="ASIGNADO|RETIRADO|EN_TRANSITO|ENTREGADO|INCIDENCIA"),
    transportista: Optional[str] = None,
    cliente: Optional[str] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_staff),
):
    rows = db.execute(SQL_PEDIDOS_FILTRO, {
        "estado": estado,
        "transportista": transportista,
        "cliente": cliente,
        "desde": str(desde) if desde else None,
        "hasta": str(hasta) if hasta else None,
    }).mappings().all()
    print(f"📡 [TRANS] api_pedidos estado={estado} transportista={transportista} cliente={cliente} -> {len(rows)}")
    return rows

@router.get("/api/incidencias", response_model=list)
def api_incidencias(
    db: Session = Depends(get_db),
    _: dict = Depends(require_staff),
):
    rows = db.execute(SQL_LIST_INCIDENCIAS).mappings().all()
    print(f"📡 [TRANS] api_incidencias -> {len(rows)}")
    return rows

# ================================
# Rutas (HTML + acciones)
# ================================
@router.get("/rutas", response_class=HTMLResponse)
def rutas_list(
    request: Request,
    fecha: Optional[date] = None,
    estado: Optional[str] = Query(default=None, description="PLANIFICADA|EN_RUTA|COMPLETADA|CANCELADA"),
    db: Session = Depends(get_db),
    staff: dict = Depends(require_staff),
):
    transps = db.execute(SQL_TRANSPORTISTAS_ACTIVOS).mappings().all()
    rutas = db.execute(SQL_RUTAS_LIST, {"fecha": str(fecha) if fecha else None, "estado": estado}).mappings().all()
    detalle_por_ruta = {}
    for r in rutas:
        det = db.execute(SQL_RUTA_DETALLE, {"id_ruta": r["id_ruta"]}).mappings().all()
        detalle_por_ruta[r["id_ruta"]] = det
    print(f"🕑 [TRANS] rutas_list fecha={fecha} estado={estado} -> {len(rutas)} rutas")
    return templates.TemplateResponse("admin_transporte_rutas.html", {
        "request": request,
        "user": staff,
        "rutas": rutas,
        "detalle_por_ruta": detalle_por_ruta,
        "transportistas": transps,
        "filtros": {"fecha": fecha, "estado": estado}
    })

@router.post("/rutas/crear")
def rutas_crear(
    fecha: date = Form(...),
    id_transportista: Optional[int] = Form(None),
    id_sucursal: Optional[int] = Form(None),
    zona: str = Form(""),
    capacidad_max: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    try:
        row = db.execute(SQL_RUTA_INSERT, {
            "fecha": str(fecha),
            "id_transportista": id_transportista,
            "id_sucursal": id_sucursal,
            "zona": zona,
            "capacidad_max": capacidad_max
        }).first()
        db.commit()
        print(f"✅ [TRANS] ruta creada id_ruta={row[0]} por={admin.get('usuario')}")
        return RedirectResponse(url=f"/admin/transporte/rutas?fecha={fecha}", status_code=303)
    except Exception as e:
        db.rollback()
        print(f"💥 [TRANS] error crear ruta: {e}")
        raise HTTPException(500, "No se pudo crear la ruta")

@router.post("/rutas/{id_ruta}/estado")
def rutas_cambiar_estado(
    id_ruta: int,
    estado: str = Form(...),  # PLANIFICADA|EN_RUTA|COMPLETADA|CANCELADA
    db: Session = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    try:
        db.execute(SQL_RUTA_SET_ESTADO, {"estado": estado, "id_ruta": id_ruta})
        db.commit()
        print(f"✅ [TRANS] ruta {id_ruta} -> estado={estado} por={admin.get('usuario')}")
        return RedirectResponse(url=f"/admin/transporte/rutas", status_code=303)
    except Exception as e:
        db.rollback()
        print(f"💥 [TRANS] error set estado ruta: {e}")
        raise HTTPException(500, "No se pudo actualizar estado")

@router.post("/rutas/{id_ruta}/add-pedido")
def rutas_add_pedido(
    id_ruta: int,
    id_pedido: int = Form(...),
    db: Session = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    try:
        db.execute(SQL_RUTADET_ADD, {"id_ruta": id_ruta, "id_pedido": id_pedido})
        db.commit()
        print(f"✅ [TRANS] ruta {id_ruta} + pedido {id_pedido}")
        return RedirectResponse(url=f"/admin/transporte/rutas", status_code=303)
    except Exception as e:
        db.rollback()
        print(f"💥 [TRANS] add pedido ruta: {e}")
        raise HTTPException(500, "No se pudo agregar pedido a la ruta")

@router.post("/rutas/{id_ruta}/del-pedido")
def rutas_del_pedido(
    id_ruta: int,
    id_pedido: int = Form(...),
    db: Session = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    try:
        db.execute(SQL_RUTADET_DEL, {"id_ruta": id_ruta, "id_pedido": id_pedido})
        db.commit()
        print(f"✅ [TRANS] ruta {id_ruta} - pedido {id_pedido}")
        return RedirectResponse(url=f"/admin/transporte/rutas", status_code=303)
    except Exception as e:
        db.rollback()
        print(f"💥 [TRANS] del pedido ruta: {e}")
        raise HTTPException(500, "No se pudo quitar pedido de la ruta")

# ================================
# Exportaciones (CSV)
# ================================
@router.get("/export/entregas.csv")
def export_entregas_csv(
    desde: date = Query(...),
    hasta: date = Query(...),
    db: Session = Depends(get_db),
    _: dict = Depends(require_staff),
):
    rows = db.execute(SQL_EXPORT_ENTREGAS, {"desde": str(desde), "hasta": str(hasta)}).mappings().all()
    print(f"🕑 [TRANS] export entregas {desde}..{hasta} -> {len(rows)} filas")

    buf = StringIO()
    buf.write("id_pedido,numero,cliente,comuna,ciudad,region,entregado_en,transportista\n")
    for r in rows:
        delivered = r["entregado_en"].strftime("%Y-%m-%d %H:%M:%S") if r["entregado_en"] else ""
        line = f'{r["id_pedido"]},{r["numero"]},"{(r["cliente"] or "").replace(",", " ")}",{r["comuna"] or ""},{r["ciudad"] or ""},{r["region"] or ""},{delivered},{r["transportista_nombre"] or ""}\n'
        buf.write(line)

    buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="entregas_{desde}_{hasta}.csv"'}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers=headers)
