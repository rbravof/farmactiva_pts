# file: app/routers/carrier.py
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional, Dict, Set
from app.database import get_db
from app.routers.admin_security import require_transportista, require_staff  
from fastapi.templating import Jinja2Templates
import os, base64, uuid

# 🔧 helper de trazas
def _dbg(tag: str, msg: str):
    print(f"{tag} {msg}")

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/carrier", tags=["Carrier"])

UPLOAD_DIR = "app/static/uploads/transporte"
os.makedirs(UPLOAD_DIR, exist_ok=True)

def _save_upload(file: UploadFile, prefix: str) -> str:
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    name = f"{prefix}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        f.write(file.file.read())
    webpath = f"/static/uploads/transporte/{name}"
    print(f"✅ [CARRIER] archivo guardado: {webpath}")
    return webpath

def _save_base64_image(b64: str, prefix: str) -> str:
    name = f"{prefix}_{uuid.uuid4().hex}.png"
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        header, _, data = b64.partition(",")  # data:image/png;base64,...
        f.write(base64.b64decode(data or b64))
    webpath = f"/static/uploads/transporte/{name}"
    print(f"✅ [CARRIER] imagen base64 guardada: {webpath}")
    return webpath

# -----------------------------
# Helpers SQL (mantenemos los de la versión anterior y añadimos algunos)
# -----------------------------
SQL_PEDIDOS_ASIGNADOS = text("""
SELECT
  p.id_pedido, p.numero, p.estado_codigo,
  COALESCE(e.nombre, p.estado_codigo) AS estado_nombre,
  p.total_neto, p.creado_en,
  c.nombre AS cliente_nombre,
  d.calle, d.numero AS calle_numero, d.depto, d.comuna, d.ciudad, d.region,
  pa.id_asignacion, t.nombre AS transportista_nombre, pa.tracking_ext, pa.estado_logistico
FROM public.pedido_asignaciones pa
JOIN public.transportistas t       ON t.id_transportista = pa.id_transportista
JOIN public.pedidos p              ON p.id_pedido = pa.id_pedido
LEFT JOIN public.pedido_estados e  ON e.codigo = p.estado_codigo
LEFT JOIN public.clientes c        ON c.id_cliente = p.id_cliente
LEFT JOIN public.direcciones_envio d ON d.id_direccion = p.id_direccion_envio
WHERE pa.activo = TRUE
  AND t.usuario = :usuario_carrier
  AND p.estado_codigo IN ('LISTO_RETIRO')
ORDER BY COALESCE(pa.actualizado_en, pa.creado_en) DESC, p.id_pedido DESC
LIMIT 200
""")

SQL_PEDIDO_HEADER = text("""
SELECT
  p.id_pedido, p.numero, p.estado_codigo,
  COALESCE(e.nombre, p.estado_codigo) AS estado_nombre,
  p.total_neto, p.costo_envio, p.creado_en,
  c.nombre AS cliente_nombre, c.telefono AS cliente_telefono, c.email AS cliente_email,
  d.calle, d.numero AS calle_numero, d.depto, d.comuna, d.ciudad, d.region,
  d.referencia AS referencia
FROM public.pedidos p
LEFT JOIN public.pedido_estados e ON e.codigo = p.estado_codigo
LEFT JOIN public.clientes c ON c.id_cliente = p.id_cliente
LEFT JOIN public.direcciones_envio d ON d.id_direccion = p.id_direccion_envio
WHERE p.id_pedido = :id
LIMIT 1
""")

SQL_EVT_LIST = text("""
SELECT id_evento, id_pedido, id_asignacion, estado, nota, actor, actor_usuario, creado_en
FROM public.pedido_envio_eventos
WHERE id_pedido = :id
ORDER BY creado_en DESC, id_evento DESC
""")

SQL_ASIG_VIGENTE = text("""
SELECT id_asignacion, id_transportista, estado_logistico, tracking_ext
FROM public.pedido_asignaciones
WHERE id_pedido = :id AND activo = TRUE
LIMIT 1
""")

# --- Bodega (origen de la ruta) ---
SQL_BODEGA_ORIGEN_DEFAULT = text("""
SELECT b.id_bodega, b.nombre, b.calle_numero, b.referencia,
       b.lat, b.lon,
       COALESCE(c.nombre, '')  AS comuna,
       COALESCE(r.nombre, '')  AS region
FROM   public.bodegas b
LEFT JOIN public.comunas  c  ON c.id_comuna  = b.id_comuna
LEFT JOIN public.regiones r  ON r.id_region  = b.id_region
WHERE  b.activo = TRUE
ORDER BY b.orden ASC, b.id_bodega ASC
LIMIT 1
""")

SQL_BODEGA_ORIGEN_BY_ID = text("""
SELECT b.id_bodega, b.nombre, b.calle_numero, b.referencia,
       b.lat, b.lon,
       COALESCE(c.nombre, '')  AS comuna,
       COALESCE(r.nombre, '')  AS region
FROM   public.bodegas b
LEFT JOIN public.comunas  c  ON c.id_comuna  = b.id_comuna
LEFT JOIN public.regiones r  ON r.id_region  = b.id_region
WHERE  b.id_bodega = :id AND b.activo = TRUE
LIMIT 1
""")

# --- Pedidos seleccionados para ruta (sin cambios si ya lo pegaste) ---
SQL_PEDIDOS_RUTA = text("""
SELECT
  p.id_pedido, p.numero,
  p.estado_codigo,
  COALESCE(e.nombre, p.estado_codigo) AS estado_nombre,
  c.nombre AS cliente_nombre,
  d.calle, d.numero AS calle_numero, d.depto, d.comuna, d.region, d.referencia
FROM public.pedidos p
JOIN public.pedido_asignaciones pa ON pa.id_pedido = p.id_pedido AND pa.activo = TRUE
JOIN public.transportistas t       ON t.id_transportista = pa.id_transportista
LEFT JOIN public.pedido_estados e  ON e.codigo = p.estado_codigo
LEFT JOIN public.clientes c        ON c.id_cliente = p.id_cliente
LEFT JOIN public.direcciones_envio d ON d.id_direccion = p.id_direccion_envio
WHERE t.usuario = :usuario_carrier
  AND p.id_pedido = ANY(:ids)
ORDER BY p.id_pedido DESC
""")

# -----------------------------
# UI Carrier
# -----------------------------
@router.get("", response_class=HTMLResponse)
def carrier_home(
    request: Request,
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    _dbg("🛡️ [CARRIER AUTH]", f"usuario='{usuario}'")

    # Validar que el usuario esté vinculado a un transportista activo
    row_t = db.execute(text("""
        SELECT id_transportista FROM public.transportistas
        WHERE usuario = :u AND activo = TRUE
        LIMIT 1
    """), {"u": usuario}).first()
    if not row_t:
        _dbg("💥 [CARRIER][LIST]", f"usuario sin transportista vinculado: {usuario}")
        raise HTTPException(status_code=403, detail="No tiene transportista asignado.")
    id_transportista = row_t[0]
    _dbg("📦 [CARRIER][LIST]", f"id_transportista={id_transportista}")

    # Ejecuta la SQL de lista (filtra por usuario del carrier + estado LISTO_RETIRO)
    rows = db.execute(SQL_PEDIDOS_ASIGNADOS, {"usuario_carrier": usuario}).mappings().all()
    _dbg("📡 [CARRIER][LIST]", f"usuario={usuario} -> {len(rows)} pedidos")
    if rows:
        r0 = rows[0]
        _dbg("✅ [CARRIER][LIST]", f"primer pedido id={r0['id_pedido']} num={r0['numero']} estado={r0['estado_codigo']} asig={r0['id_asignacion']}")
    else:
        _dbg("🟡 [CARRIER][LIST]", "sin pedidos (verifica asignación activa y estado LISTO_RETIRO)")

    return templates.TemplateResponse("carrier_pedidos_list.html", {
        "request": request,
        "carrier_user": carrier_user,
        "rows": rows,      # compat
        "pedidos": rows,   # alias para el template
    })

@router.get("/pedidos/{id_pedido}", response_class=HTMLResponse)
def carrier_pedido_detalle(
    id_pedido: int,
    request: Request,
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    _dbg("🔎 [CARRIER][DET]", f"usuario={usuario} id_pedido={id_pedido}")

    # validar asignación vigente del usuario
    row_t = db.execute(text("""
        SELECT t.id_transportista
        FROM public.transportistas t
        WHERE t.usuario = :u AND t.activo = TRUE
        LIMIT 1
    """), {"u": usuario}).first()
    if not row_t:
        _dbg("💥 [CARRIER][DET]", "usuario sin transportista activo")
        raise HTTPException(403, "No tiene transportista asignado.")
    id_transportista = row_t[0]

    asig = db.execute(text("""
        SELECT pa.id_asignacion
        FROM public.pedido_asignaciones pa
        WHERE pa.id_pedido = :p AND pa.id_transportista = :t AND pa.activo = TRUE
        LIMIT 1
    """), {"p": id_pedido, "t": id_transportista}).first()
    if not asig:
        _dbg("💥 [CARRIER][DET]", f"pedido {id_pedido} no asignado a este transportista")
        raise HTTPException(404, "Pedido no asignado a este transportista.")

    header = db.execute(SQL_PEDIDO_HEADER, {"id": id_pedido}).mappings().first()
    if not header:
        _dbg("💥 [CARRIER][DET]", f"pedido {id_pedido} no encontrado")
        raise HTTPException(404, "Pedido no encontrado")

    eventos = db.execute(SQL_EVT_LIST, {"id": id_pedido}).mappings().all()
    _dbg("📜 [CARRIER][DET]", f"eventos={len(eventos)}")
    return templates.TemplateResponse("carrier_pedido_detalle.html", {
        "request": request, "header": header, "eventos": eventos, "carrier_user": carrier_user
    })

# -----------------------------
# Eventos/Transiciones
# -----------------------------
def _insert_envio_evento(db: Session, id_pedido: int, id_asignacion: Optional[int], estado: str, nota: Optional[str], actor_usuario: Optional[str], actor="transportista"):
    db.execute(text("""
        INSERT INTO public.pedido_envio_eventos (id_pedido, id_asignacion, estado, nota, actor, actor_usuario, creado_en)
        VALUES (:p, :a, :e, :n, :actor, :u, now())
    """), {"p": id_pedido, "a": id_asignacion, "e": estado, "n": (nota or "").strip() or None, "u": actor_usuario, "actor": actor})
    print(f"✅ [CARRIER][EVT] id_pedido={id_pedido} asig={id_asignacion} estado={estado} usuario={actor_usuario}")

def _transition_if_allowed(db: Session, id_pedido: int, nuevo_estado: str, actor_usuario: Optional[str]):
    cur = db.execute(text("SELECT estado_codigo FROM public.pedidos WHERE id_pedido=:id"), {"id": id_pedido}).scalar()
    allowed = db.execute(text("""
        SELECT dest.codigo
        FROM public.pedido_estado_transiciones t
        JOIN public.pedido_estados orig ON orig.id_estado = t.origen
        JOIN public.pedido_estados dest ON dest.id_estado = t.destino
        WHERE UPPER(orig.codigo) = UPPER(:cur) AND t.activo = TRUE AND dest.activo = TRUE
    """), {"cur": cur}).scalars().all()
    if allowed and nuevo_estado not in allowed:
        print(f"💥 [CARRIER] transición denegada cur={cur} -> {nuevo_estado}")
        return False

    db.execute(text("UPDATE public.pedidos SET estado_codigo=:e WHERE id_pedido=:id"), {"e": nuevo_estado, "id": id_pedido})
    db.execute(text("""
        INSERT INTO public.pedido_estado_historial (id_pedido, estado_origen, estado_destino, nota, audiencia, destinatario_rol, created_by, creado_en)
        VALUES (:id, (SELECT id_estado FROM public.pedido_estados WHERE codigo=:cur),
                     (SELECT id_estado FROM public.pedido_estados WHERE codigo=:dst),
                     :nota, 'NEXT_ROLE', NULL,
                     (SELECT id FROM public.usuarios WHERE usuario=:u), now())
    """), {"id": id_pedido, "cur": cur, "dst": nuevo_estado, "nota": f"Carrier {actor_usuario or ''}", "u": (actor_usuario or "")})
    print(f"✅ [CARRIER] transición OK id_pedido={id_pedido} {cur} -> {nuevo_estado}")
    return True

def _get_asignacion_vigente(db: Session, id_pedido: int):
    row = db.execute(SQL_ASIG_VIGENTE, {"id": id_pedido}).mappings().first()
    _dbg("🔗 [CARRIER][ASIG]", f"id_pedido={id_pedido} -> {('id_asignacion=' + str(row['id_asignacion'])) if row else 'NO VIGENTE'}")
    return row

@router.post("/pedidos/{id_pedido}/marcar-retirado")
def carrier_marcar_retirado(
    id_pedido: int, nota: str = Form(""),
    db: Session = Depends(get_db), carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    _dbg("📝 [CARRIER][RETIRADO]", f"user={usuario} pedido={id_pedido}")
    asig = _get_asignacion_vigente(db, id_pedido)
    if not asig: raise HTTPException(404, "Pedido sin asignación vigente.")
    _insert_envio_evento(db, id_pedido, asig.get("id_asignacion"), "RETIRADO", nota, usuario)
    _transition_if_allowed(db, id_pedido, "RETIRADO", usuario)  # ← AQUÍ el cambio
    db.commit()
    return RedirectResponse(url=f"/carrier/pedidos/{id_pedido}", status_code=303)

@router.post("/pedidos/{id_pedido}/marcar-en-transito")
def carrier_marcar_en_transito(
    id_pedido: int, nota: str = Form(""),
    db: Session = Depends(get_db), carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    _dbg("📝 [CARRIER][TRANSITO]", f"user={usuario} pedido={id_pedido}")
    asig = _get_asignacion_vigente(db, id_pedido)
    if not asig: raise HTTPException(404, "Pedido sin asignación vigente.")
    _insert_envio_evento(db, id_pedido, asig.get("id_asignacion"), "EN_TRANSITO", nota, usuario)
    _transition_if_allowed(db, id_pedido, "EN_TRANSITO", usuario)
    db.commit()
    return RedirectResponse(url=f"/carrier/pedidos/{id_pedido}", status_code=303)

@router.post("/pedidos/{id_pedido}/marcar-entregado")
def carrier_marcar_entregado(
    id_pedido: int, receptor_nombre: str = Form(""), nota: str = Form(""),
    db: Session = Depends(get_db), carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    _dbg("📝 [CARRIER][ENTREGADO]", f"user={usuario} pedido={id_pedido} receptor='{receptor_nombre}'")
    asig = _get_asignacion_vigente(db, id_pedido)
    if not asig: raise HTTPException(404, "Pedido sin asignación vigente.")
    txt = ("Entregado a: " + (receptor_nombre or "").strip()) + ((" — " + nota.strip()) if (nota or "").strip() else "")
    _insert_envio_evento(db, id_pedido, asig.get("id_asignacion"), "ENTREGADO", txt, usuario)
    _transition_if_allowed(db, id_pedido, "ENTREGADO", usuario)
    db.commit()
    return RedirectResponse(url=f"/carrier/pedidos/{id_pedido}", status_code=303)

# -----------------------------
# Nuevos endpoints: GPS, Temperatura, Incidencia, Devolución, Firma
# -----------------------------
@router.post("/gps/ping")
def carrier_gps_ping(
    id_pedido: int = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    acc_m: float = Form(0),
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")

    # 1) Validación de pertenencia
    vinc = db.execute(text("""
        SELECT pa.id_transportista
        FROM public.pedido_asignaciones pa
        JOIN public.transportistas t ON t.id_transportista = pa.id_transportista
        WHERE pa.id_pedido = :p AND pa.activo = TRUE AND t.usuario = :u
        LIMIT 1
    """), {"p": id_pedido, "u": usuario}).mappings().first()
    if not vinc:
        print(f"💥 [GPS] DENEGADO pedido={id_pedido} usuario={usuario} (sin asignación activa)")
        raise HTTPException(status_code=403, detail="No autorizado para este pedido")

    id_transportista = vinc["id_transportista"]

    # 2) Insert + commit con trazas fuertes
    try:
        res = db.execute(text("""
            INSERT INTO public.pedido_gps_pings (id_pedido, id_transportista, lat, lon, acc_m, fuente)
            VALUES (:p, :t, :lat, :lon, :acc, 'html5')
        """), {"p": id_pedido, "t": id_transportista, "lat": lat, "lon": lon, "acc": acc_m})
        db.commit()
        print(f"🕑 [GPS] ping OK pedido={id_pedido} t={id_transportista} lat={lat} lon={lon} acc={acc_m} rows={res.rowcount}")
    except Exception as e:
        db.rollback()
        print(f"💥 [GPS] ERROR insert pedido={id_pedido}: {e}")
        raise HTTPException(status_code=500, detail="No se pudo registrar el ping")

    # 3) Broadcast en tiempo real (si hay listeners)
    try:
        _room_broadcast(id_pedido, {
            "type": "gps_ping",
            "id_pedido": id_pedido,
            "lat": float(lat),
            "lon": float(lon),
            "acc_m": float(acc_m),
        })
    except Exception as e:
        # no aborta el flujo si falla el WS
        print(f"⚠️ [GPS] broadcast falló pedido={id_pedido}: {e}")

    return {"ok": True}


@router.post("/temperatura/registrar")
def carrier_temp_registrar(
    id_pedido: int = Form(...), celsius: float = Form(...), sensor_id: str = Form(""),
    db: Session = Depends(get_db), carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    asig = _get_asignacion_vigente(db, id_pedido)
    if not asig: raise HTTPException(404, "Pedido sin asignación.")
    dentro = 1 if (2.0 <= celsius <= 8.0) else 0  # rango cadena de frío típico
    db.execute(text("""
        INSERT INTO public.temp_registros (id_asignacion, sensor_id, celsius, dentro_rango, creado_en)
        VALUES (:a, NULLIF(:sid,''), :c, :ok, now())
    """), {"a": asig["id_asignacion"], "sid": sensor_id, "c": celsius, "ok": dentro})
    if not dentro:
        _insert_envio_evento(db, id_pedido, asig["id_asignacion"], "TEMP_ALERT",
                             f"Temperatura fuera de rango: {celsius}°C", usuario, actor="sistema")
    db.commit()
    print(f"🕑 [CARRIER] temp a={asig['id_asignacion']} c={celsius}°C dentro={bool(dentro)}")
    return {"ok": True, "dentro_rango": bool(dentro)}

@router.post("/pedidos/{id_pedido}/incidencia")
def carrier_reportar_incidencia(
    id_pedido: int, tipo: str = Form(...), descripcion: str = Form(""),
    foto: UploadFile | None = File(None),
    db: Session = Depends(get_db), carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    asig = _get_asignacion_vigente(db, id_pedido)
    if not asig: raise HTTPException(404, "Pedido sin asignación.")
    url = _save_upload(foto, "incidencia") if foto else None
    db.execute(text("""
        INSERT INTO public.incidencias (id_asignacion, tipo, descripcion, foto_url, creado_por, creado_en)
        VALUES (:a, :t, NULLIF(:d,''), :url, :u, now())
    """), {"a": asig["id_asignacion"], "t": tipo, "d": descripcion, "url": url, "u": usuario})
    _insert_envio_evento(db, id_pedido, asig["id_asignacion"], "INCIDENCIA", f"{tipo} — {descripcion}", usuario)
    db.commit()
    print(f"💥 [CARRIER] incidencia id_pedido={id_pedido} tipo={tipo}")
    return RedirectResponse(url=f"/carrier/pedidos/{id_pedido}", status_code=303)

@router.post("/pedidos/{id_pedido}/devolucion")
def carrier_registrar_devolucion(
    id_pedido: int, motivo: str = Form(""), foto: UploadFile | None = File(None),
    db: Session = Depends(get_db), carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    asig = _get_asignacion_vigente(db, id_pedido)
    if not asig: raise HTTPException(404, "Pedido sin asignación.")
    url = _save_upload(foto, "devolucion") if foto else None
    db.execute(text("""
        INSERT INTO public.devoluciones (id_asignacion, motivo, foto_url, creado_por, creado_en)
        VALUES (:a, NULLIF(:m,''), :url, :u, now())
    """), {"a": asig["id_asignacion"], "m": motivo, "url": url, "u": usuario})
    _insert_envio_evento(db, id_pedido, asig["id_asignacion"], "INCIDENCIA", f"DEVOLUCION — {motivo}", usuario)
    db.commit()
    print(f"💥 [CARRIER] devolucion id_pedido={id_pedido} motivo={motivo}")
    return RedirectResponse(url=f"/carrier/pedidos/{id_pedido}", status_code=303)

@router.post("/pedidos/{id_pedido}/firmar")
def carrier_firmar_entrega(
    id_pedido: int, receptor_nombre: str = Form(""), firma_b64: str = Form(...),
    db: Session = Depends(get_db), carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    asig = _get_asignacion_vigente(db, id_pedido)
    if not asig: raise HTTPException(404, "Pedido sin asignación.")
    url = _save_base64_image(firma_b64, "firma")
    db.execute(text("""
        INSERT INTO public.firmas_entrega (id_asignacion, receptor_nombre, imagen_url, creado_en)
        VALUES (:a, NULLIF(:r,''), :url, now())
    """), {"a": asig["id_asignacion"], "r": receptor_nombre, "url": url})
    _insert_envio_evento(db, id_pedido, asig["id_asignacion"], "ENTREGADO", f"Firma de {receptor_nombre}", usuario)
    _transition_if_allowed(db, id_pedido, "ENTREGADO", usuario)
    db.commit()
    print(f"✅ [CARRIER] firma registrada id_pedido={id_pedido}")
    return RedirectResponse(url=f"/carrier/pedidos/{id_pedido}", status_code=303)

@router.get("/debug", response_class=JSONResponse)
def carrier_debug_list(
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    rows = db.execute(SQL_PEDIDOS_ASIGNADOS, {"usuario_carrier": usuario}).mappings().all()
    _dbg("🔬 [CARRIER][DEBUG-LIST]", f"usuario={usuario} rows={len(rows)}")
    return {"usuario": usuario, "count": len(rows), "items": rows}

@router.get("/debug/raw", response_class=JSONResponse)
def carrier_debug_raw(
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    # Mismo SQL, pero devolvemos solo ids y estado para compactar
    rows = db.execute(text("""
        SELECT p.id_pedido, p.estado_codigo, pa.id_asignacion
        FROM public.pedido_asignaciones pa
        JOIN public.transportistas t ON t.id_transportista = pa.id_transportista
        JOIN public.pedidos p ON p.id_pedido = pa.id_pedido
        WHERE pa.activo = TRUE
          AND t.usuario = :usuario
          AND p.estado_codigo IN ('LISTO_RETIRO')
        ORDER BY COALESCE(pa.actualizado_en, pa.creado_en) DESC, p.id_pedido DESC
        LIMIT 200
    """), {"usuario": usuario}).mappings().all()
    _dbg("🔬 [CARRIER][DEBUG-RAW]", f"usuario={usuario} rows={len(rows)}")
    return {"usuario": usuario, "count": len(rows), "items": rows}

@router.get("/ruta")
def carrier_build_route(
    ids: str,
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    """
    Devuelve un Google Maps Directions URL con origen = bodega
    y destino + waypoints = direcciones de los pedidos seleccionados.
    """
    usuario = (carrier_user or {}).get("usuario")

    # Parsear ids CSV
    try:
        id_list = [int(x) for x in (ids or "").split(",") if x.strip().isdigit()]
    except Exception:
        id_list = []
    if not id_list:
        return JSONResponse({"ok": False, "error": "ids vacíos"}, status_code=400)

    # 1) Bodega de origen (usa tu bodega id=1; puedes ajustar la query si tienes multi-sucursal)
    bodega = db.execute(text("""
        SELECT calle_numero, COALESCE(referencia,'') AS referencia,
               COALESCE(id_comuna,0) AS id_comuna, COALESCE(id_region,0) AS id_region
        FROM public.bodegas
        WHERE id_bodega = 1
        LIMIT 1
    """)).mappings().first()
    if not bodega:
        return JSONResponse({"ok": False, "error": "Bodega no encontrada"}, status_code=500)

    # 2) Direcciones de los pedidos (valida que estén asignados al transportista logueado)
    rows = db.execute(text("""
        SELECT p.id_pedido,
               d.calle, d.numero AS calle_numero, d.depto, d.comuna, d.region, d.referencia
        FROM public.pedido_asignaciones pa
        JOIN public.transportistas t ON t.id_transportista = pa.id_transportista
        JOIN public.pedidos p        ON p.id_pedido = pa.id_pedido
        LEFT JOIN public.direcciones_envio d ON d.id_direccion = p.id_direccion_envio
        WHERE pa.activo = TRUE
          AND t.usuario = :u
          AND p.id_pedido = ANY(:ids)
        ORDER BY p.id_pedido
    """), {"u": usuario, "ids": id_list}).mappings().all()

    if not rows:
        return JSONResponse({"ok": False, "error": "Pedidos no válidos para este usuario"}, status_code=403)

    # 3) Construir las cadenas de dirección texto
    def _fmt_dir(r):
        linea1 = " ".join([str(r.get("calle") or ""), str(r.get("calle_numero") or "")]).strip()
        comuna_region = ", ".join([x for x in [r.get("comuna"), r.get("region")] if x])
        compl = (r.get("depto") or "")  # no forzamos referencia aquí (es opcional para el routing)
        parts = [linea1]
        if comuna_region:
            parts.append(comuna_region)
        if compl:
            parts.append(compl)
        parts.append("Chile")
        return ", ".join([p for p in parts if p])

    origin = ", ".join([p for p in [bodega["calle_numero"], "Chile"] if p])
    stops  = [_fmt_dir(r) for r in rows]

    if not stops:
        return JSONResponse({"ok": False, "error": "Sin direcciones de destino"}, status_code=400)

    # 4) Armar URL de Google Maps Directions (orden actual; optimización real la hacemos luego)
    #    https://www.google.com/maps/dir/?api=1&origin=...&destination=...&waypoints=w1|w2|...
    destination = stops[-1]
    waypoints   = stops[:-1]
    from urllib.parse import quote

    url = "https://www.google.com/maps/dir/?api=1" \
          + "&origin=" + quote(origin) \
          + "&destination=" + quote(destination)
    if waypoints:
        url += "&waypoints=" + quote("|".join(waypoints))

    return {"ok": True, "count": len(stops), "gmaps_url": url}


# --- Hub en memoria por pedido ---
_ws_rooms: Dict[int, Set[WebSocket]] = {}  # pedido -> set(sockets)

def _room_add(id_pedido: int, ws: WebSocket):
    _ws_rooms.setdefault(id_pedido, set()).add(ws)

def _room_remove(id_pedido: int, ws: WebSocket):
    try:
        _ws_rooms.get(id_pedido, set()).discard(ws)
        if _ws_rooms.get(id_pedido) and len(_ws_rooms[id_pedido]) == 0:
            _ws_rooms.pop(id_pedido, None)
    except Exception:
        pass

def _room_broadcast(id_pedido: int, payload: dict):
    for ws in list(_ws_rooms.get(id_pedido, set())):
        try:
            ws.send_json(payload)
        except Exception:
            _room_remove(id_pedido, ws)

# --- POST /carrier/gps/ping (extiende el tuyo) ---
@router.post("/gps/ping")
def carrier_gps_ping(
    id_pedido: int = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    acc_m: float = Form(0),
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")
    # valida que el pedido pertenezca al transportista logueado
    row = db.execute(text("""
        SELECT pa.id_transportista
        FROM public.pedido_asignaciones pa
        JOIN public.transportistas t ON t.id_transportista = pa.id_transportista
        WHERE pa.id_pedido = :p AND pa.activo = TRUE AND t.usuario = :u
        LIMIT 1
    """), {"p": id_pedido, "u": usuario}).mappings().first()
    if not row:
        print(f"💥 [GPS] ping DENEGADO pedido={id_pedido} usuario={usuario}")
        raise HTTPException(status_code=403, detail="No autorizado")

    id_transportista = db.execute(text("""
        SELECT id_transportista FROM public.transportistas WHERE usuario=:u
    """), {"u": usuario}).scalar()

    # persiste ping
    db.execute(text("""
        INSERT INTO public.pedido_gps_pings (id_pedido, id_transportista, lat, lon, acc_m, fuente)
        VALUES (:p, :t, :lat, :lon, :acc, 'html5')
    """), {"p": id_pedido, "t": id_transportista, "lat": lat, "lon": lon, "acc": acc_m})
    db.commit()
    print(f"🕑 [GPS] ping OK pedido={id_pedido} lat={lat} lon={lon} acc={acc_m}")

    # emite en tiempo real (si hay listeners)
    _room_broadcast(id_pedido, {
        "type": "gps_ping",
        "id_pedido": id_pedido,
        "lat": float(lat), "lon": float(lon),
        "acc_m": float(acc_m),
    })
    return {"ok": True}

# --- GET último GPS (fallback polling) ---
@router.get("/gps/ult/{id_pedido}")
def carrier_gps_last(
    id_pedido: int,
    db: Session = Depends(get_db),
    user: dict = Depends(require_staff)  # o crear un require_carrier_or_staff
):
    row = db.execute(text("""
        SELECT id_pedido, id_transportista, lat, lon, acc_m, creado_en
        FROM public.v_pedido_gps_ultimo
        WHERE id_pedido = :p
        LIMIT 1
    """), {"p": id_pedido}).mappings().first()
    return row or {}

# --- WebSocket: canal por pedido ---
@router.websocket("/gps/ws/{id_pedido}")
async def ws_gps(request: Request, websocket: WebSocket, id_pedido: int):
    # Handshake
    await websocket.accept()
    _room_add(id_pedido, websocket)
    print(f"📡 [GPS WS] conectado pedido={id_pedido} total={len(_ws_rooms.get(id_pedido, []))}")
    try:
        while True:
            # opcional: recibir pings desde el cliente (no necesario; usamos POST)
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        _room_remove(id_pedido, websocket)
        print(f"📴 [GPS WS] desconectado pedido={id_pedido}")
    except Exception as e:
        _room_remove(id_pedido, websocket)
        print(f"💥 [GPS WS] error pedido={id_pedido}: {e}")

# -----------------------------
# Rutas
# -----------------------------
@router.get("/ruta", response_class=HTMLResponse)
def carrier_plan_ruta(
    request: Request,
    ids: str = "",                      # ej: "12,13,14"
    bodega: int | None = None,          # opcional: forzar id_bodega
    origin: str | None = None,          # opcional: texto manual (fallback)
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    usuario = (carrier_user or {}).get("usuario")

    # IDs de pedidos seleccionados
    try:
        sel_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    except Exception:
        sel_ids = []
    if not sel_ids:
        print("💥 [CARRIER][RUTA] sin ids seleccionados")
        raise HTTPException(status_code=400, detail="Debes seleccionar al menos un pedido.")

    # Origen: BODEGA (por defecto) o por parámetro ?bodega=
    if bodega:
        row_bod = db.execute(SQL_BODEGA_ORIGEN_BY_ID, {"id": bodega}).mappings().first()
    else:
        row_bod = db.execute(SQL_BODEGA_ORIGEN_DEFAULT).mappings().first()

    origin_lat = origin_lon = None
    origin_label = ""
    if row_bod:
        origin_lat = float(row_bod["lat"]) if row_bod["lat"] is not None else None
        origin_lon = float(row_bod["lon"]) if row_bod["lon"] is not None else None
        origin_label = " · ".join([
            f"{row_bod['calle_numero']}".strip(),
            f"{(row_bod['comuna'] or '').strip()}".strip(),
            f"{(row_bod['region'] or '').strip()}".strip()
        ]).strip(" ·")
        # Si hay referencia, la agregamos abajo del label (en la vista)
    else:
        print("🟡 [CARRIER][RUTA] no hay bodega activa; se permitirá origen manual")
        origin_label = (origin or "").strip()

    # Pedidos
    rows = db.execute(SQL_PEDIDOS_RUTA, {
        "usuario_carrier": usuario,
        "ids": sel_ids
    }).mappings().all()

    print(f"🧭 [CARRIER][RUTA] usuario={usuario} ids={sel_ids} -> rows={len(rows)} bodega={row_bod['id_bodega'] if row_bod else 'N/A'}")

    return templates.TemplateResponse("carrier_ruta_plan.html", {
        "request": request,
        "carrier_user": carrier_user,
        "origin": origin_label,       # texto visible en el input
        "origin_lat": origin_lat,     # coords desde bodega (si existen)
        "origin_lon": origin_lon,
        "origin_ref": (row_bod or {}).get("referencia") or "",
        "origin_nombre": (row_bod or {}).get("nombre") or "Bodega",
        "pedidos": rows,
    })

@router.get("/api/pedidos/seleccion", response_class=JSONResponse)
def carrier_api_pedidos_seleccion(
    ids: str = "",
    db: Session = Depends(get_db),
    carrier_user: dict = Depends(require_transportista),
):
    """Devuelve JSON con los pedidos seleccionados (para debug o integraciones)."""
    usuario = (carrier_user or {}).get("usuario")
    try:
        sel_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    except Exception:
        sel_ids = []
    if not sel_ids:
        return JSONResponse({"ok": True, "items": []})

    rows = db.execute(SQL_PEDIDOS_RUTA, {
        "usuario_carrier": usuario,
        "ids": sel_ids
    }).mappings().all()

    items = []
    for r in rows:
        items.append({
            "id_pedido": r["id_pedido"],
            "numero": r["numero"],
            "cliente": r["cliente_nombre"],
            "dir1": " ".join([str(r["calle"] or ""), str(r["calle_numero"] or "")]).strip(),
            "comuna": r["comuna"],
            "region": r["region"],
            "ref": r["referencia"],
        })
    print(f"📡 [CARRIER][API RUTA] usuario={usuario} -> {len(items)} items")
    return JSONResponse({"ok": True, "items": items})

