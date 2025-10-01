# app/routers/admin_pedidos.py
from __future__ import annotations
from app.utils.emailer import send_email
from fastapi import APIRouter, Depends, Request, Form, Query, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from app.models import Pedido, PedidoItem, PedidoNota, PedidoHistorial, Usuario, UsuarioRol
from starlette.datastructures import FormData
from app.database import get_db
from sqlalchemy.orm import Session
from datetime import datetime
import random, re
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from sqlalchemy import text, select
from typing import Optional, Dict
from sqlalchemy.sql import func
# Usa el mismo guard que en otros routers admin
from app.routers.admin_security import require_staff
from app.utils.view import render_admin

# === Pago ===
import os
try:
    import mercadopago  # type: ignore
except Exception:
    mercadopago = None  # type: ignore


templates = Jinja2Templates(directory="app/templates")

BASE_URL_ADMIN = os.getenv("BASE_URL_ADMIN", "http://127.0.0.1:8002").rstrip("/")

# ⚠️ IMPORTANTE: este 'router' es el que espera main.py
router = APIRouter(
    prefix="/admin/pedidos",
    tags=["Admin Pedidos"],
    dependencies=[Depends(require_staff)]
)


def _append_pedido_historial(db, id_pedido: int, estado_origen: str, estado_destino: str):
    """
    Inserta una fila en historial de estados. Intenta con el modelo ORM
    (tabla 'pedido_historial') y, si no existe, hace fallback a la tabla
    'pedido_estado_historial' por SQL crudo.
    """
    print(f"[HIST] id_pedido={id_pedido} origen={estado_origen} -> destino={estado_destino}")

    # 1) Intento con el modelo ORM (tabla: pedido_historial)
    try:
        rec = PedidoHistorial(
            id_pedido=id_pedido,
            estado_origen=estado_origen,
            estado_destino=estado_destino,
        )
        db.add(rec)
        db.flush()  # para forzar la inserción ahora
        print("[HIST] Insert ORM OK (pedido_historial)")
        return
    except Exception as ex:
        # Puede fallar si la tabla mapeada no existe en la BD (nombres distintos)
        print(f"[HIST] ORM insert falló: {ex}. Reintentando por SQL crudo…")

    # 2) Fallback: tabla alternativa 'pedido_estado_historial'
    try:
        db.execute(
            text("""
                INSERT INTO public.pedido_estado_historial
                    (id_pedido, estado_origen, estado_destino)
                VALUES (:id_pedido, :estado_origen, :estado_destino)
            """),
            {
                "id_pedido": id_pedido,
                "estado_origen": estado_origen,
                "estado_destino": estado_destino,
            },
        )
        print("[HIST] Insert SQL OK (pedido_estado_historial)")
    except Exception as ex2:
        print(f"[HIST] Fallback SQL también falló: {ex2}")
        # lo dejamos propagar para que veas el error y alinear nombres de tabla
        raise

@router.get("/")
def admin_pedidos_list(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
):
    # ¿Existe la tabla pedido_notas? (devuelve True/False)
    has_notas = bool(db.execute(text("""
        SELECT EXISTS (
          SELECT 1
          FROM information_schema.tables
          WHERE table_schema = 'public' AND table_name = 'pedido_notas'
        )
    """)).scalar())

    # SELECT base (común a ambos casos)
    base_select = """
        SELECT
          p.id_pedido,
          p.numero,
          p.estado_codigo,
          COALESCE(e.nombre, p.estado_codigo) AS estado_nombre,
          p.total_neto,
          p.creado_en,
          p.canal,
          -- nombre del cliente para la columna "Cliente"
          COALESCE(NULLIF(c.nombre, ''), CAST(p.id_cliente AS TEXT)) AS cliente_nombre
        {extra_cols}
        FROM public.pedidos p
        LEFT JOIN public.pedido_estados e ON e.codigo = p.estado_codigo
        LEFT JOIN public.clientes       c ON c.id_cliente = p.id_cliente
        {extra_joins}
        ORDER BY p.id_pedido DESC
        LIMIT 100
    """

    if has_notas:
        SQL = text(base_select.format(
            extra_cols="""
                ,(SELECT n.texto
                    FROM public.pedido_notas n
                    WHERE n.id_pedido = p.id_pedido
                ORDER BY n.creado_en DESC
                    LIMIT 1) AS ultima_nota
                ,(SELECT n.audiencia
                    FROM public.pedido_notas n
                    WHERE n.id_pedido = p.id_pedido
                ORDER BY n.creado_en DESC
                    LIMIT 1) AS ultima_audiencia
                ,(SELECT n.destinatario_rol
                    FROM public.pedido_notas n
                    WHERE n.id_pedido = p.id_pedido
                ORDER BY n.creado_en DESC
                    LIMIT 1) AS ultima_nota_para
            """,
            extra_joins=""
        ))

    else:
        # versión sin referencias a pedido_notas (evita UndefinedTable)
        SQL = text(base_select.format(
            extra_cols="""
              ,NULL::text AS ultima_nota
              ,NULL::text AS ultima_audiencia
              ,NULL::text AS ultima_nota_para
            """,
            extra_joins=""
        ))

    # Ejecutamos con un retry ligero por si la sesión viene abortada
    try:
        rows = db.execute(SQL).mappings().all()
    except SQLAlchemyError:
        try:
            db.rollback()
        except Exception:
            pass
        rows = db.execute(SQL).mappings().all()

    flash_success = None
    if request.query_params.get("ok") == "created":
        n = request.query_params.get("n", "el pedido")
        flash_success = f"✅ Pedido {n} creado correctamente."

    ctx = {"rows": rows, "estados": [], "flash_success": flash_success}
    return render_admin(templates, request, "admin_pedidos_list.html", ctx, admin_user)

@router.get("/nuevo")
def admin_pedidos_new(request: Request, admin_user: dict = Depends(require_staff)):
    return render_admin(templates, request, "admin_pedidos_form.html", {}, admin_user)

@router.post("/nuevo")
async def admin_pedidos_new_submit(
    request: Request,
    id_cliente: str = Form(None),
    canal: str = Form("manual"),
    accion: str = Form("solo_crear"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
    background_tasks: BackgroundTasks = None,
):
    OFFSET_BASE = 1000

    trc = f"trc-{datetime.utcnow().strftime('%H%M%S')}-{random.randint(1000,9999)}"
    print(f"[PEDIDOS/NUEVO][{trc}] ===> Inicio submit de nuevo pedido")

    def _int_or_none(v):
        try:
            return int(v) if v not in (None, "", "null") else None
        except Exception:
            return None

    def _parse_money_clp(v: str) -> int:
        if not v:
            return 0
        n = "".join(ch for ch in str(v) if ch.isdigit())
        return int(n) if n else 0

    try:
        form: FormData = await request.form()

        # ---- Datos del paso 2 ----
        id_cli = _int_or_none(form.get("id_cliente"))
        id_tipo_envio = _int_or_none(form.get("id_tipo_envio"))
        id_direccion = _int_or_none(form.get("id_direccion"))
        envio_raw = form.get("envio_clp")
        costo_envio = _parse_money_clp(envio_raw)

        print(f"[PEDIDOS/NUEVO][{trc}] Admin={(admin_user or {}).get('email','(desconocido)')}  accion='{accion}'  canal='{canal}'")
        print(f"[PEDIDOS/NUEVO][{trc}] Cliente id={id_cli}  tipo_envio={id_tipo_envio}  direccion={id_direccion}  envio_raw='{envio_raw}' -> envio={costo_envio}")

        # ---- Si NO hay id_direccion pero sí hay datos de dirección, crearla en direcciones_envio ----
        if not id_direccion:
            def _get_env(k: str) -> str:
                # soporta names del form como envio[nombre] o nombre
                return (form.get(f"envio[{k}]") or form.get(k) or "").strip()

            # IDs opcionales (si el formulario los envía)
            id_comuna_form  = _int_or_none(_get_env("id_comuna"))
            id_region_form  = _int_or_none(_get_env("id_region"))

            envio = {
                "id_cliente":  id_cli,
                "nombre":      _get_env("nombre") or None,
                "telefono":    (_get_env("telefono") or None),
                "calle":       _get_env("calle") or None,
                "numero":      _get_env("numero") or None,
                "depto":       _get_env("depto") or None,
                "comuna":      _get_env("comuna") or None,   # texto (compat)
                "ciudad":      _get_env("ciudad") or None,
                "region":      _get_env("region") or None,   # texto (compat)
                "referencia":  _get_env("referencia") or None,
                "id_comuna":   id_comuna_form,
                "id_region":   id_region_form,
            }

            # Resolver IDs por nombre si no vinieron y tenemos texto
            # (ajusta nombres de tablas/campos si tu catálogo es distinto)
            try:
                if envio["id_comuna"] is None and envio["comuna"]:
                    row = db.execute(text("""
                        SELECT id_comuna FROM public.comunas 
                        WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(:n))
                        LIMIT 1
                    """), {"n": envio["comuna"]}).first()
                    if row:
                        envio["id_comuna"] = row[0]
                        print(f"[PEDIDOS/NUEVO][{trc}] id_comuna resuelto por nombre='{envio['comuna']}' -> {envio['id_comuna']}")
                if envio["id_region"] is None and envio["region"]:
                    row = db.execute(text("""
                        SELECT id_region FROM public.regiones 
                        WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(:n))
                        LIMIT 1
                    """), {"n": envio["region"]}).first()
                    if row:
                        envio["id_region"] = row[0]
                        print(f"[PEDIDOS/NUEVO][{trc}] id_region resuelto por nombre='{envio['region']}' -> {envio['id_region']}")
            except Exception as e:
                print(f"[PEDIDOS/NUEVO][{trc}] WARN resolviendo IDs comuna/region: {e}")

            # criterio mínimo para considerar que hay datos reales de dirección
            tiene_datos = any([envio["calle"], envio["comuna"], envio["ciudad"], envio["nombre"]])
            print(f"[PEDIDOS/NUEVO][{trc}] Datos dirección detectados: {envio} -> tiene_datos={tiene_datos}")

            if tiene_datos:
                # Inserta incluyendo id_comuna/id_region (si tu tabla ya los tiene)
                try:
                    res = db.execute(text("""
                        INSERT INTO public.direcciones_envio
                          (id_cliente, nombre, telefono, calle, numero, depto, comuna, ciudad, region, referencia, id_comuna, id_region)
                        VALUES
                          (:id_cliente, :nombre, :telefono, :calle, :numero, :depto, :comuna, :ciudad, :region, :referencia, :id_comuna, :id_region)
                        RETURNING id_direccion
                    """), envio)
                    id_direccion = res.scalar()
                    print(f"[PEDIDOS/NUEVO][{trc}] Dirección creada id={id_direccion} (con IDs)")
                except Exception as e:
                    # Fallback: si aún no agregaste las columnas id_comuna/id_region, probamos sin ellas
                    db.rollback()
                    print(f"[PEDIDOS/NUEVO][{trc}] WARN insert con IDs falló ({e}); reintento sin id_comuna/id_region")
                    res = db.execute(text("""
                        INSERT INTO public.direcciones_envio
                          (id_cliente, nombre, telefono, calle, numero, depto, comuna, ciudad, region, referencia)
                        VALUES
                          (:id_cliente, :nombre, :telefono, :calle, :numero, :depto, :comuna, :ciudad, :region, :referencia)
                        RETURNING id_direccion
                    """), envio)
                    id_direccion = res.scalar()
                    print(f"[PEDIDOS/NUEVO][{trc}] Dirección creada id={id_direccion} (sin IDs)")
            else:
                print(f"[PEDIDOS/NUEVO][{trc}] No se detectaron datos suficientes de dirección; no se inserta en direcciones_envio.")

        # ---- Ítems (paso 1) ----
        ids_prod   = form.getlist("items[][id_producto]") or form.getlist("items[id_producto]")
        precios_br = form.getlist("items[][precio_unitario]") or form.getlist("items[precio_unitario]")
        cant_list  = form.getlist("items[][cantidad]") or form.getlist("items[cantidad]")
        d_tipo     = form.getlist("items[][descuento_tipo]") or form.getlist("items[descuento_tipo]")
        d_valor    = form.getlist("items[][descuento_valor]") or form.getlist("items[descuento_valor]")

        print(f"[PEDIDOS/NUEVO][{trc}] Items recibidos: ids={ids_prod}  precios={precios_br}  cantidades={cant_list}  desc_tipo={d_tipo}  desc_valor={d_valor}")

        if not ids_prod:
            raise HTTPException(status_code=400, detail="El pedido debe tener al menos 1 ítem.")

        items_netos = []
        subtotal_items_neto = 0

        for idx, sid in enumerate(ids_prod):
            id_prod = _int_or_none(sid)
            if not id_prod:
                continue

            try:
                cantidad = int(cant_list[idx]) if idx < len(cant_list) else 1
            except Exception:
                cantidad = 1
            if cantidad < 1:
                cantidad = 1

            try:
                precio_u_bruto = int(precios_br[idx])
            except Exception:
                precio_u_bruto = 0
            if precio_u_bruto < 0:
                precio_u_bruto = 0

            tipo = (d_tipo[idx] if idx < len(d_tipo) else "monto") or "monto"
            try:
                dv = int(d_valor[idx]) if idx < len(d_valor) else 0
            except Exception:
                dv = 0
            dv = max(0, dv)

            total_bruto = cantidad * precio_u_bruto
            if tipo == "porcentaje":
                pct = min(100, dv)
                desc_total = (total_bruto * pct) // 100
            else:
                desc_total = min(dv, total_bruto)

            subtotal_neto_fila = max(0, total_bruto - desc_total)
            precio_u_neto = subtotal_neto_fila // cantidad if cantidad else precio_u_bruto

            print(f"[PEDIDOS/NUEVO][{trc}] - Ítem {idx}: id={id_prod} qty={cantidad} pu_bruto={precio_u_bruto} "
                  f"tipo_desc={tipo} val_desc={dv} total_bruto={total_bruto} desc={desc_total} "
                  f"sub_neto_fila={subtotal_neto_fila} pu_neto={precio_u_neto}")

            items_netos.append(
                {
                    "id_producto": id_prod,
                    "cantidad": cantidad,
                    "precio_unitario": precio_u_neto,
                    "subtotal": precio_u_neto * cantidad,
                }
            )
            subtotal_items_neto += (precio_u_neto * cantidad)

        if not items_netos:
            raise HTTPException(status_code=400, detail="No se pudo interpretar ningún ítem válido.")

        print(f"[PEDIDOS/NUEVO][{trc}] Subtotal ítems (neto) calculado: {subtotal_items_neto}")

        # ---- Estado inicial ----
        row = db.execute(text("SELECT 1 FROM public.pedido_estados WHERE codigo = :c LIMIT 1"),
                         {"c": "pendiente_pago"}).first()
        estado_inicial = "pendiente_pago" if row else "NUEVO"
        print(f"[PEDIDOS/NUEVO][{trc}] Estado inicial: {estado_inicial}")

        # ---- Totales ----
        total_neto = subtotal_items_neto + int(costo_envio or 0)
        print(f"[PEDIDOS/NUEVO][{trc}] Totales: subtotal_items={subtotal_items_neto} envio={costo_envio} total_neto={total_neto}")

        # ---- Insert pedido (número temporal) ----
        print(f"[PEDIDOS/NUEVO][{trc}] Insertando pedido…")
        pedido = Pedido(
            numero="tmp",
            id_cliente=id_cli,
            canal=(canal or "manual"),
            estado_codigo=estado_inicial,
            id_tipo_envio=id_tipo_envio,
            id_direccion_envio=id_direccion,  # <- viene del insert en direcciones_envio (si se creó)
            costo_envio=int(costo_envio or 0),
            total_neto=int(total_neto),
        )
        db.add(pedido)
        db.flush()  # id_pedido

        # ---- Número corto basado en id ----
        pedido.numero = f"#{OFFSET_BASE + int(pedido.id_pedido)}"
        print(f"[PEDIDOS/NUEVO][{trc}] Número asignado: {pedido.numero} (id={pedido.id_pedido})")

        # ---- Cache de nombres de producto ----
        print(f"[PEDIDOS/NUEVO][{trc}] Recuperando nombres de productos…")
        nombres_cache: dict[int, str] = {}
        for it in items_netos:
            pid = it["id_producto"]
            if pid in nombres_cache:
                continue
            nombre = db.execute(
                text("SELECT titulo FROM public.productos WHERE id_producto = :id LIMIT 1"),
                {"id": pid}
            ).scalar()
            if not nombre:
                nombre = f"Producto {pid}"
            nombres_cache[pid] = str(nombre)

        # ---- Insert detalle ----
        for it in items_netos:
            db.add(PedidoItem(
                id_pedido=pedido.id_pedido,
                id_producto=it["id_producto"],
                nombre_producto=nombres_cache[it["id_producto"]],
                cantidad=it["cantidad"],
                precio_unitario=it["precio_unitario"],
                subtotal=it["subtotal"],
            ))
        print(f"[PEDIDOS/NUEVO][{trc}] Ítems insertados: {len(items_netos)}")

        # ---- Historial (si hay modelo compatible) ----
        hist_field = next((f for f in ("estado_codigo", "estado_destino", "estado", "estado_nuevo")
                           if hasattr(PedidoHistorial, f)), None)
        if hist_field:
            kw = {hist_field: estado_inicial, "id_pedido": pedido.id_pedido}
            db.add(PedidoHistorial(**kw))
            print(f"[PEDIDOS/NUEVO][{trc}] Historial insertado usando campo '{hist_field}'='{estado_inicial}'")
        else:
            print(f"[PEDIDOS/NUEVO][{trc}] Aviso: No se encontró un campo de estado en PedidoHistorial; se omite historial.")

        # ---- Nota opcional ----
        obs = (form.get("observacion") or "").strip()
        if obs:
            nota_kwargs = {"texto": obs, "id_pedido": pedido.id_pedido}
            if hasattr(PedidoNota, "audiencia"):
                nota_kwargs["audiencia"] = "INTERNAL_ALL"
            if hasattr(PedidoNota, "visible_para_cliente"):
                nota_kwargs["visible_para_cliente"] = False
            nota_state_field = next((f for f in ("estado_codigo_destino", "estado_destino", "estado")
                                     if hasattr(PedidoNota, f)), None)
            if nota_state_field:
                nota_kwargs[nota_state_field] = estado_inicial
                print(f"[PEDIDOS/NUEVO][{trc}] Nota con estado en '{nota_state_field}'='{estado_inicial}'")
            db.add(PedidoNota(**nota_kwargs))

        # ---- Commit ----
        db.commit()
        print(f"[PEDIDOS/NUEVO][{trc}] ✅ Commit OK. Pedido id={pedido.id_pedido} numero={pedido.numero}")

        # ---- Notificación a QF si el pedido nació en NUEVO ----
        try:
            if (estado_inicial or "").upper() == "NUEVO":
                _notify_qf_new_order(db, pedido.id_pedido, pedido.numero)
        except Exception as e:
            print(f"[MAIL][QF][{trc}] Error notificando a QF: {e}")

        # ===================== ACCIÓN: CREAR Y ENVIAR LINK =====================
        if (accion or "").lower() == "crear_enviar_link":
            print(f"[PEDIDOS/NUEVO][{trc}] Generando cobro y enviando link por correo…")

            # 1) datos del cliente
            cli = db.execute(text("""
                SELECT nombre, email
                  FROM public.clientes
                 WHERE id_cliente = :id
                 LIMIT 1
            """), {"id": id_cli}).mappings().first() or {}
            email_to = (cli.get("email") or "").strip()

            # 2) insertar pendiente
            id_pago = None
            try:
                id_pago = db.execute(text("""
                    INSERT INTO public.pedido_pagos (id_pedido, proveedor, link_url, monto, moneda, estado)
                    VALUES (:p, 'MercadoPago', NULL, :monto, 'CLP', 'pendiente')
                    RETURNING id_pago
                """), {"p": pedido.id_pedido, "monto": int(total_neto)}).scalar_one()
                db.commit()
                print(f"[pagos/mp][{trc}] pedido_pagos INSERT pendiente -> id_pago={id_pago}")
            except Exception as e:
                db.rollback()
                print(f"[pagos/mp][{trc}] ERROR insert pedido_pagos:", e)

            # 3) crear preferencia y guardar link
            link_url = None
            if id_pago:
                try:
                    from app.services.mercadopago import create_mp_preference
                    pref = create_mp_preference(
                        id_pago=id_pago,
                        id_pedido=pedido.id_pedido,
                        numero_fmt=pedido.numero,   # ej "#1010"
                        monto=int(total_neto),
                        moneda="CLP",
                        email_to=email_to or None,
                    )
                    link_url = pref.get("init_point")
                    if not link_url:
                        raise RuntimeError("MercadoPago no devolvió init_point")
                    db.execute(text("UPDATE public.pedido_pagos SET link_url=:u WHERE id_pago=:id"),
                               {"u": link_url, "id": id_pago})
                    db.commit()
                    print(f"[pagos/mp][{trc}] UPDATE link_url OK -> {link_url}")
                except Exception as e:
                    db.rollback()
                    print(f"[pagos/mp][{trc}] ERROR creando preferencia MP:", e)

            # 4) enviar correo (si hay email)
            try:
                if email_to:
                    from app.utils.emailer import send_email
                    asunto = f"Solicitud de pago pedido {pedido.numero}"
                    html = f"""
                        <h2>Pago pendiente</h2>
                        <p>Hola {(cli.get('nombre') or '').strip()},</p>
                        <p>Generamos el cobro por <strong>{int(total_neto)} CLP</strong> del pedido <strong>{pedido.numero}</strong>.</p>
                        {f'<p><a href="{link_url}" target="_blank" rel="noopener" style="display:inline-block;padding:10px 14px;background:#4f46e5;color:#fff;border-radius:8px;text-decoration:none;">Pagar ahora</a></p>' if link_url else '<p>No pudimos generar un enlace automático. Te contactaremos para coordinar el pago.</p>'}
                    """.strip()
                    text_alt = f"Pago pendiente por {int(total_neto)} CLP del pedido {pedido.numero}.\n" + (f"Pagar ahora: {link_url}" if link_url else "")
                    ok = send_email(email_to, asunto, html, text_alt)
                    print(f"[pagos/email][{trc}] send_email -> {ok}")
                else:
                    print(f"[pagos/email][{trc}] Cliente sin email; se omite envío.")
            except Exception as e:
                print(f"[pagos/email][{trc}] ERROR enviando correo:", e)

            # 5) nota visible al cliente
            try:
                nota_kwargs = {
                    "id_pedido": pedido.id_pedido,
                    "texto": f"Se envió solicitud de pago por {int(total_neto)} CLP a {email_to or '(sin email)'}"
                             + (f" con link {link_url}" if link_url else " (sin link)"),
                }
                if hasattr(PedidoNota, "audiencia"):
                    nota_kwargs["audiencia"] = "cliente"
                if hasattr(PedidoNota, "autor_nombre"):
                    nota_kwargs["autor_nombre"] = (admin_user or {}).get("nombre") or "admin"
                if hasattr(PedidoNota, "autor_rol"):
                    nota_kwargs["autor_rol"] = "admin"
                db.add(PedidoNota(**nota_kwargs))
                db.commit()
                print(f"[PEDIDOS/NUEVO][{trc}] Nota registrada (cliente).")
            except Exception as e:
                db.rollback()
                print(f"[PEDIDOS/NUEVO][{trc}] WARN al registrar nota:", e)

            # 6) Redirigir al detalle del pedido
            return RedirectResponse(url=f"/admin/pedidos/{pedido.id_pedido}?sent=1", status_code=303)

        # ===================== FLUJO NORMAL: SOLO CREAR =====================
        return RedirectResponse(
            url=f"/admin/pedidos?ok=created&n={pedido.numero}",
            status_code=303
        )

    except HTTPException:
        raise
    except IntegrityError as e:
        db.rollback()
        print(f"[PEDIDOS/NUEVO][{trc}] IntegrityError: {e}")
        raise HTTPException(status_code=400, detail="No fue posible guardar el pedido (integridad).")
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[PEDIDOS/NUEVO][{trc}] SQLAlchemyError: {e}")
        raise HTTPException(status_code=500, detail="No fue posible guardar el pedido.")
    except Exception as e:
        db.rollback()
        print(f"[PEDIDOS/NUEVO][{trc}] Excepción no controlada: {e}")
        raise HTTPException(status_code=500, detail="Error inesperado al crear el pedido.")

# =========================================================
# JSON usados por el frontend
# =========================================================
@router.get("/flujo")
def admin_pedidos_flujo(admin_user: dict = Depends(require_staff)):
    # Mermaid de ejemplo; pronto se generará desde BD.
    # RECHAZADO_QF ahora es final (sin flechas de salida) y con estilo "terminal".
    mermaid = """
    flowchart LR
      classDef terminal fill:#fee2e2,stroke:#ef4444,color:#7f1d1d,stroke-width:2;

      NUEVO["Nuevo (QF)"]
      APROBADO_QF["Aprobado QF (Preparación)"]
      APROBADO_CRITERIO_QF["Aprobado Criterio QF (Preparación)"]
      RECHAZADO_QF["Rechazado QF (Final)"]:::terminal
      EN_PREPARACION["En Preparación (Logística)"]
      EN_TRANSITO["En Tránsito (Logística)"]
      ENTREGADO["Entregado (Atención)"]
      RECHAZADO_CLIENTE["Rechazado Cliente (Atención)"]

      NUEVO --> APROBADO_QF
      NUEVO --> APROBADO_CRITERIO_QF
      NUEVO --> RECHAZADO_QF

      APROBADO_QF --> EN_PREPARACION
      APROBADO_CRITERIO_QF --> EN_PREPARACION

      EN_PREPARACION --> EN_TRANSITO
      EN_TRANSITO --> ENTREGADO
      EN_TRANSITO --> RECHAZADO_CLIENTE
    """.strip()
    return JSONResponse({"ok": True, "mermaid": mermaid})

from fastapi.responses import JSONResponse
from sqlalchemy import text

@router.get("/{id_pedido}/siguientes-estados")
def admin_pedidos_siguientes_estados(
    id_pedido: int,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
):
    cur = db.execute(
        text("SELECT estado_codigo FROM public.pedidos WHERE id_pedido = :id"),
        {"id": id_pedido}
    ).scalar()
    if not cur:
        return JSONResponse({"ok": False, "error": "Pedido no encontrado"}, status_code=404)

    items = _next_states_for(db, cur)
    return JSONResponse({"ok": True, "items": items})

@router.post("/{id_pedido}/cambiar-estado")
def admin_pedidos_cambiar_estado(
    id_pedido: int,
    nuevo_estado: str = Form(...),
    nota_cliente: str | None = Form(None),
    nota_rol: str | None = Form(None),
    destinatario_rol: str | None = Form(None),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
):
    print(f"[pedidos/actions] cambiar estado id={id_pedido} -> {nuevo_estado}")

    def _clean(s): return s.strip() if isinstance(s, str) and s.strip() else None
    nota_cliente = _clean(nota_cliente)
    nota_rol = _clean(nota_rol)
    destinatario_rol = _clean(destinatario_rol)

    if not nuevo_estado or len(nuevo_estado) > 64:
        return JSONResponse({"ok": False, "error": "estado inválido"}, status_code=400)

    # Código del estado actual
    estado_actual = db.execute(
        text("SELECT estado_codigo FROM public.pedidos WHERE id_pedido = :id"),
        {"id": id_pedido},
    ).scalar()
    print(f"[pedidos/actions] estado_actual={estado_actual!r}")

    # Validación transición (si aplica)
    allowed = _next_states_for(db, estado_actual)
    allowed_codes = {e["codigo"] for e in allowed}
    if allowed_codes and nuevo_estado not in allowed_codes:
        return JSONResponse({"ok": False, "error": "Transición no permitida"}, status_code=400)

    # IDs de catálogo (ORIGEN y DESTINO)
    id_estado_destino = db.execute(
        text("SELECT id_estado FROM public.pedido_estados WHERE codigo = :c"),
        {"c": nuevo_estado},
    ).scalar()
    if not id_estado_destino:
        return JSONResponse({"ok": False, "error": "Estado destino no existe en catálogo"}, status_code=400)

    id_estado_origen = None
    if estado_actual:
        id_estado_origen = db.execute(
            text("SELECT id_estado FROM public.pedido_estados WHERE codigo = :c"),
            {"c": estado_actual},
        ).scalar()

    # id del actor para created_by
    actor_usuario = (admin_user or {}).get("usuario")
    created_by_id = db.execute(
        text("SELECT id FROM public.usuarios WHERE usuario = :u"),
        {"u": actor_usuario},
    ).scalar()

    # Nota obligatoria para historial
    nota_hist = nota_rol or nota_cliente or f"Cambio de estado de {estado_actual or '—'} a {nuevo_estado}"

    try:
        # 1) Actualizar estado del pedido
        db.execute(
            text("""
                UPDATE public.pedidos
                   SET estado_codigo = :estado
                 WHERE id_pedido = :id
            """),
            {"estado": nuevo_estado, "id": id_pedido},
        )

        # 2) Insertar historial con ORIGEN + DESTINO
        db.execute(
            text("""
                INSERT INTO public.pedido_estado_historial
                    (id_pedido, estado_origen, estado_destino, nota, audiencia, destinatario_rol, created_by, creado_en)
                VALUES
                    (:id_pedido, :id_origen, :id_destino, :nota,
                     COALESCE(:audiencia, 'NEXT_ROLE'::audiencia_nota),
                     :destinatario, :created_by, now())
            """),
            {
                "id_pedido": id_pedido,
                "id_origen": id_estado_origen,
                "id_destino": id_estado_destino,
                "nota": nota_hist,
                "audiencia": None,
                "destinatario": destinatario_rol,
                "created_by": created_by_id,
            },
        )

        # 3) Notas opcionales (como antes)
        try:
            autor_nombre = (admin_user or {}).get("nombre") or actor_usuario or "admin"
            db.execute(
                text("""
                    INSERT INTO public.pedido_notas
                        (id_pedido, texto, audiencia, creado_en, autor_nombre, autor_rol)
                    VALUES
                        (:id, :texto, 'interno', now(), :autor, 'admin')
                """),
                {"id": id_pedido, "texto": f"Estado cambiado de {estado_actual or '—'} a {nuevo_estado}", "autor": autor_nombre},
            )
            if nota_cliente:
                db.execute(
                    text("""
                        INSERT INTO public.pedido_notas
                            (id_pedido, texto, audiencia, creado_en, autor_nombre, autor_rol)
                        VALUES
                            (:id, :texto, 'cliente', now(), :autor, 'admin')
                    """),
                    {"id": id_pedido, "texto": nota_cliente, "autor": autor_nombre},
                )
            if nota_rol or destinatario_rol:
                db.execute(
                    text("""
                        INSERT INTO public.pedido_notas
                            (id_pedido, texto, audiencia, destinatario_rol, creado_en, autor_nombre, autor_rol)
                        VALUES
                            (:id, :texto, 'interno', :destino, now(), :autor, 'admin')
                    """),
                    {
                        "id": id_pedido,
                        "texto": nota_rol or (f"Instrucciones para {destinatario_rol}" if destinatario_rol else "Instrucciones"),
                        "destino": destinatario_rol,
                        "autor": autor_nombre,
                    },
                )
        except Exception as e_notes:
            print(f"[pedidos/actions] notas opcionales omitidas: {e_notes}")

        # 4) Acción especial si el destino es RECHAZADO_QF: archivar + notificar cliente
        if (nuevo_estado or "").upper() == "RECHAZADO_QF":
            print(f"🧩 [pedidos/actions] Post-acción por destino=RECHAZADO_QF id={id_pedido}")

            # 4.1) Intentar marcar como 'histórico' si existen columnas archivado/archivado_en
            try:
                res_arch = db.execute(text("""
                    UPDATE public.pedidos
                       SET archivado = TRUE,
                           archivado_en = now()
                     WHERE id_pedido = :id
                """), {"id": id_pedido})
                print(f"🗄️ [pedidos/actions] archivado={res_arch.rowcount} filas afectadas (si 0, puede que no existan columnas)")
            except Exception as e_arch:
                print(f"⚠️ [pedidos/actions] No se pudo marcar histórico (puede no existir columna): {e_arch}")

            # 4.2) Notificar al cliente por email (si hay email)
            try:
                cli = db.execute(text("""
                    SELECT p.numero, c.email, c.nombre
                      FROM public.pedidos p
                      JOIN public.clientes c ON c.id_cliente = p.id_cliente
                     WHERE p.id_pedido = :id
                     LIMIT 1
                """), {"id": id_pedido}).mappings().first() or {}

                numero_fmt = (cli.get("numero") or f"#{id_pedido}")
                email_to   = (cli.get("email") or "").strip()
                cli_nombre = (cli.get("nombre") or "").strip()

                if email_to:
                    try:
                        from app.utils.emailer import send_email
                        asunto = f"Pedido {numero_fmt} rechazado por QF"
                        html = f"""
                            <h2>Pedido rechazado</h2>
                            <p>Hola {cli_nombre or 'cliente'},</p>
                            <p>Tu pedido <strong>{numero_fmt}</strong> fue <strong>rechazado por nuestro Químico Farmacéutico</strong> tras la revisión correspondiente.</p>
                            <p>Si tienes dudas, responde este correo para ayudarte.</p>
                        """.strip()
                        text_alt = f"Pedido {numero_fmt} rechazado por QF."
                        ok = send_email(email_to, asunto, html, text_alt)
                        print(f"📧 [pedidos/actions] Correo rechazo-> {ok} to={email_to}")
                    except Exception as e_mail:
                        print(f"💥 [pedidos/actions] Error enviando correo de rechazo: {e_mail}")
                else:
                    print("ℹ️ [pedidos/actions] Cliente sin email; no se envía notificación.")
            except Exception as e_cli:
                print(f"⚠️ [pedidos/actions] No se pudo obtener datos de cliente para aviso: {e_cli}")

            # 4.3) Registrar nota para cliente (explicando rechazo)
            try:
                autor_nombre = (admin_user or {}).get("nombre") or actor_usuario or "admin"
                texto_cli = "Tu pedido fue rechazado por QF tras la revisión correspondiente. Si tienes dudas, contáctanos."
                db.execute(text("""
                    INSERT INTO public.pedido_notas
                        (id_pedido, texto, audiencia, creado_en, autor_nombre, autor_rol)
                    VALUES
                        (:id, :texto, 'cliente', now(), :autor, 'admin')
                """), {"id": id_pedido, "texto": texto_cli, "autor": autor_nombre})
                print("📝 [pedidos/actions] Nota para cliente registrada por rechazo QF")
            except Exception as e_nc:
                print(f"⚠️ [pedidos/actions] No se pudo insertar nota de cliente: {e_nc}")

        # Commit final
        db.commit()
        print("[pedidos/actions] cambio de estado OK + historial + notas (+ post-acción si aplica)")
        return JSONResponse({"ok": True, "nuevo_estado": nuevo_estado})
    except Exception as e:
        db.rollback()
        print(f"[pedidos/actions] ERROR cambiando estado: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# =========================================================
# Asignar Transportista
# =========================================================
@router.post("/{id_pedido}/asignar-transportista")
def admin_pedido_asignar_transportista(
    id_pedido: int,
    id_transportista: int = Form(...),
    tracking_ext: str = Form(""),
    obs: str = Form(""),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
):
    actor_usuario = (admin_user or {}).get("usuario")
    print(f"[CARRIER][ASSIGN] id_pedido={id_pedido} -> id_transportista={id_transportista} by={actor_usuario}")

    # Validaciones básicas
    ped = db.execute(text("SELECT id_pedido FROM public.pedidos WHERE id_pedido=:id"), {"id": id_pedido}).first()
    if not ped:
        return JSONResponse({"ok": False, "error": "Pedido no existe"}, status_code=404)
    tr = db.execute(text("""
        SELECT id_transportista, nombre, activo FROM public.transportistas WHERE id_transportista=:t
    """), {"t": id_transportista}).mappings().first()
    if not tr or not tr.get("activo", False):
        return JSONResponse({"ok": False, "error": "Transportista inválido o inactivo"}, status_code=400)

    try:
        # 1) cerrar asignación vigente si existe
        db.execute(text("""
            UPDATE public.pedido_asignaciones
               SET activo = FALSE, estado_logistico = 'ASIGNADO', actualizado_en = now()
             WHERE id_pedido = :p AND activo = TRUE
        """), {"p": id_pedido})

        # 2) crear nueva asignación activa
        row = db.execute(text("""
            INSERT INTO public.pedido_asignaciones (id_pedido, id_transportista, estado_logistico, asignado_por, tracking_ext, obs, activo)
            VALUES (:p, :t, 'ASIGNADO', :who, NULLIF(:trk,''), NULLIF(:obs,''), TRUE)
            RETURNING id_asignacion
        """), {"p": id_pedido, "t": id_transportista, "who": actor_usuario, "trk": tracking_ext, "obs": obs}).first()
        id_asig = row[0]

        # 3) evento logístico
        db.execute(text("""
            INSERT INTO public.pedido_envio_eventos (id_pedido, id_asignacion, estado, nota, actor, actor_usuario)
            VALUES (:p, :a, 'ASIGNADO', :n, 'admin', :who)
        """), {"p": id_pedido, "a": id_asig, "n": f"Asignado a {tr['nombre']}", "who": actor_usuario})

        # 4) nota interna para el timeline
        try:
            db.execute(text("""
                INSERT INTO public.pedido_notas (id_pedido, autor_nombre, autor_rol, audiencia, texto, creado_en)
                VALUES (:p, :autor, 'admin', 'interno', :txt, now())
            """), {"p": id_pedido, "autor": (admin_user or {}).get("nombre") or actor_usuario or "admin",
                   "txt": f"Pedido asignado a {tr['nombre']} ({tr['id_transportista']})."})
        except Exception as e_notes:
            print(f"[CARRIER][ASSIGN] warn notas: {e_notes}")

        db.commit()
        print(f"[CARRIER][ASSIGN] ✅ OK id_pedido={id_pedido} id_asignacion={id_asig}")
        return RedirectResponse(url=f"/admin/pedidos/{id_pedido}?ok=asignado", status_code=303)
    except Exception as e:
        db.rollback()
        print(f"[CARRIER][ASSIGN] 💥 ERROR: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# =========================================================
# Integración Mercado Pago – preferencia, callback, webhook
# =========================================================
def _get_mp_client():
    """Devuelve el SDK de Mercado Pago o un mensaje de error si no está disponible."""
    if mercadopago is None:
        return None, "SDK de Mercado Pago no está instalado. Ejecuta: pip install mercadopago"

    access_token = os.getenv("MP_ACCESS_TOKEN") or os.getenv("MERCADOPAGO_ACCESS_TOKEN")
    if not access_token:
        return None, "Falta MP_ACCESS_TOKEN (o MERCADOPAGO_ACCESS_TOKEN) en variables de entorno"

    try:
        sdk = mercadopago.SDK(access_token)  # type: ignore[attr-defined]
        return sdk, None
    except Exception as e:
        return None, f"No fue posible inicializar SDK MP: {e}"

@router.get("/pagos/mercadopago/callback")
def mp_callback_collector(
    status: str = Query(None),
    payment_id: str = Query(None, alias="payment_id"),
    preference_id: str = Query(None, alias="preference_id"),
    external_reference: str = Query(None),
    admin_user: dict = Depends(require_staff),
):
    # Simplemente redirigimos al listado con un pequeño estado en QS
    url = f"/admin/pedidos?mp_status={status or ''}&payment_id={payment_id or ''}&pref={preference_id or ''}"
    return RedirectResponse(url=url, status_code=303)


@router.post("/mercadopago/webhook")
async def mp_webhook(payload: dict):
    # Para pruebas: solo echo. En producción: validar firma/secret si corresponde y actualizar pedido.
    print("[MP webhook] payload:", payload)
    return {"ok": True}

# =========================================================
# Etiqueta Envio
# =========================================================
@router.get("/{id_pedido}/etiqueta", response_class=HTMLResponse)
def admin_pedido_etiqueta(
    id_pedido: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
):
    print(f"[pedidos/etiqueta] GET id={id_pedido}")

    header = db.execute(SQL_PEDIDO_HEADER, {"id": id_pedido}).mappings().first()
    if not header:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    envio = fetch_envio_direccion(db, header.get("id_direccion_envio"), header.get("id_cliente"))

    direccion = {
        "linea1": ("{} {}".format((envio or {}).get("calle",""), (envio or {}).get("numero","")).strip() or None),
        "linea2": (envio or {}).get("depto") or None,
        "comuna": (envio or {}).get("comuna") or None,
        "ciudad": (envio or {}).get("ciudad") or None,
    }
    cliente = {
        "nombre": header.get("cliente_nombre"),
        "telefono": header.get("cliente_telefono"),
    }
    etiqueta_codigo = (header.get("numero") or f"#{1000+int(header['id_pedido'])}").replace("#","")

    return templates.TemplateResponse("etiqueta_envio.html", {
        "request": request,
        "pedido": header,
        "cliente": cliente,
        "direccion": direccion,
        "etiqueta_codigo": etiqueta_codigo,
    })

# =========================================================
# Detalle Pedido
# =========================================================
def _adapt_address(row: Dict) -> Dict:
    """Normaliza distintas columnas posibles a un formato común."""
    g = row.get
    return {
        "nombre":         g("nombre") or g("contacto") or g("full_name") or g("recipient"),
        "calle":          g("calle")  or g("direccion") or g("linea1") or g("address1"),
        "numero":         g("numero") or g("num") or g("nro") or "",
        "depto":          g("depto")  or g("departamento") or g("linea2") or g("address2"),
        "comuna":         g("comuna") or g("localidad") or g("barrio"),
        "ciudad":         g("ciudad") or g("poblacion") or g("municipio"),
        "region":         g("region") or g("provincia") or g("estado"),
        "codigo_postal":  g("codigo_postal") or g("zip") or g("postal") or g("cp"),
        "pais":           g("pais") or g("country"),
        "telefono":       g("telefono") or g("phone"),
    }

def _try_fetch_by(db, table: str, key: str, id_val: int) -> Optional[Dict]:
    """Intenta SELECT * FROM table WHERE key=:id; devuelve mapping dict o None."""
    try:
        sql = text(f"SELECT * FROM public.{table} WHERE {key} = :id LIMIT 1")
        row = db.execute(sql, {"id": id_val}).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None

def _split_calle_numero(calle_numero: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Separa 'Av. X 680B' → ('Av. X', '680B'). Si no hay número, devuelve (texto, None)."""
    s = (calle_numero or "").strip()
    if not s:
        return None, None
    # intenta separar por último bloque con dígitos (permite sufijos tipo 680B, 680-1)
    m = re.match(r"^(.*?)[,\s]+(\d[\dA-Za-z\-\/]*)$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, None

def fetch_envio_direccion(db, id_dir: Optional[int], id_cliente: Optional[int]):
    """
    Resolución de dirección para la UI:
    1) Busca en public.direcciones_envio por id_direccion.
    2) Si no, busca en public.clientes_direcciones por id_direccion (mapeando campos).
    3) Si no, última de direcciones_envio por id_cliente.
    4) Si no, última de clientes_direcciones por id_cliente (mapeando).
    """
    print(f"[pedidos] fetch_envio_direccion id_dir={id_dir} id_cliente={id_cliente}")

    # 1) por id_direccion en direcciones_envio
    if id_dir:
        row = db.execute(text("""
            SELECT id_direccion, id_cliente, nombre, telefono, calle, numero, depto,
                   comuna, ciudad, region, referencia
              FROM public.direcciones_envio
             WHERE id_direccion = :id
             LIMIT 1
        """), {"id": id_dir}).mappings().first()
        if row:
            d = dict(row)
            d.setdefault("codigo_postal", None)
            d.setdefault("pais", "Chile")
            return d

    # 2) fallback por id_direccion en clientes_direcciones
    if id_dir:
        cd = db.execute(text("""
            SELECT id_direccion, id_cliente, calle_numero, depto, referencia
              FROM public.clientes_direcciones
             WHERE id_direccion = :id
             LIMIT 1
        """), {"id": id_dir}).mappings().first()
        if cd:
            calle, numero = _split_calle_numero(cd.get("calle_numero"))
            return {
                "id_direccion": cd["id_direccion"],
                "id_cliente": cd.get("id_cliente"),
                "nombre": None,
                "telefono": None,
                "calle": calle,
                "numero": numero,
                "depto": cd.get("depto"),
                "comuna": None,
                "ciudad": None,
                "region": None,
                "codigo_postal": None,
                "pais": "Chile",
                "referencia": cd.get("referencia"),
            }

    # 3) última por cliente en direcciones_envio
    if id_cliente:
        row = db.execute(text("""
            SELECT id_direccion, id_cliente, nombre, telefono, calle, numero, depto,
                   comuna, ciudad, region, referencia
              FROM public.direcciones_envio
             WHERE id_cliente = :id_cliente
             ORDER BY creado_en DESC, id_direccion DESC
             LIMIT 1
        """), {"id_cliente": id_cliente}).mappings().first()
        if row:
            d = dict(row)
            d.setdefault("codigo_postal", None)
            d.setdefault("pais", "Chile")
            return d

    # 4) última por cliente en clientes_direcciones
    if id_cliente:
        cd_last = db.execute(text("""
            SELECT id_direccion, id_cliente, calle_numero, depto, referencia
              FROM public.clientes_direcciones
             WHERE id_cliente = :id_cliente
             ORDER BY id_direccion DESC
             LIMIT 1
        """), {"id_cliente": id_cliente}).mappings().first()
        if cd_last:
            calle, numero = _split_calle_numero(cd_last.get("calle_numero"))
            return {
                "id_direccion": cd_last["id_direccion"],
                "id_cliente": cd_last.get("id_cliente"),
                "nombre": None,
                "telefono": None,
                "calle": calle,
                "numero": numero,
                "depto": cd_last.get("depto"),
                "comuna": None,
                "ciudad": None,
                "region": None,
                "codigo_postal": None,
                "pais": "Chile",
                "referencia": cd_last.get("referencia"),
            }

    # Nada encontrado
    return None

# --- Fuente única de verdad: transiciones en BD ---
def _next_states_for(db, estado_actual: str | None) -> list[dict]:
    """
    Devuelve la lista de estados destino válidos para un estado actual (por código).
    Lee desde public.pedido_estado_transiciones (origen/destino por id).
    """
    if not estado_actual:
        return []
    
    if (estado_actual or "").upper() == "RECHAZADO_QF":
        return []  # estado final, no tiene siguientes


    sql = text("""
        SELECT
            dest.codigo AS codigo,
            COALESCE(NULLIF(dest.nombre, ''), dest.codigo) AS nombre,
            dest.rol_responsable
        FROM public.pedido_estado_transiciones t
        JOIN public.pedido_estados orig ON orig.id_estado = t.origen
        JOIN public.pedido_estados dest ON dest.id_estado = t.destino
        WHERE UPPER(orig.codigo) = UPPER(:cur)
          AND t.activo = TRUE
          AND dest.activo = TRUE
        ORDER BY dest.orden NULLS LAST, dest.codigo
    """)

    try:
        rows = db.execute(sql, {"cur": estado_actual}).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:
        # Fallback defensivo si la tabla no existe o hay un problema puntual
        print(f"[pedidos/_next_states_for] error consultando transiciones: {e}")
        sql_fallback = text("""
            SELECT codigo,
                   COALESCE(NULLIF(nombre,''), codigo) AS nombre,
                   rol_responsable
            FROM public.pedido_estados
            WHERE activo = TRUE AND UPPER(codigo) <> UPPER(:cur)
            ORDER BY orden NULLS LAST, codigo
        """)
        return [dict(r) for r in db.execute(sql_fallback, {"cur": estado_actual}).mappings().all()]

SQL_PEDIDO_HEADER = text("""
SELECT
    p.id_pedido,
    p.numero,
    p.estado_codigo,
    COALESCE(e.nombre, p.estado_codigo) AS estado_nombre,
    p.total_neto,
    p.costo_envio,
    p.creado_en,
    p.canal,
    p.id_cliente,
    p.id_tipo_envio,
    p.id_direccion_envio,

    -- 👇 campos de pago que faltaban
    p.pago_estado,
    p.pago_proveedor,
    p.pago_monto,
    p.pago_moneda,

    te.nombre AS tipo_envio_nombre,
    c.nombre   AS cliente_nombre,
    c.email    AS cliente_email,
    c.telefono AS cliente_telefono
FROM public.pedidos p
LEFT JOIN public.pedido_estados e ON e.codigo = p.estado_codigo
LEFT JOIN public.clientes       c ON c.id_cliente = p.id_cliente
LEFT JOIN public.tipos_envio    te ON te.id_tipo_envio = p.id_tipo_envio
WHERE p.id_pedido = :id
""")

# --- SQL de items (usa nombre guardado en el ítem; si no, el título del producto) ---
SQL_PEDIDO_ITEMS = text("""
  SELECT
    i.id_item, i.id_pedido, i.id_producto,
    i.nombre_producto, i.cantidad, i.precio_unitario, i.subtotal,
    COALESCE(NULLIF(i.nombre_producto, ''), pr.titulo) AS display_nombre_producto,
    pr.imagen_principal_url
  FROM pedido_items i
  LEFT JOIN productos pr ON pr.id_producto = i.id_producto
  WHERE i.id_pedido = :id
  ORDER BY i.id_item
""")

def _notify_qf_new_order(db: Session, id_pedido: int, numero: str | None = None) -> None:
    """
    Notifica a todos los QF activos que existe un nuevo pedido para revisión.
    En dev: si falta configuración SMTP, solo hace print().
    """
    q = (
        select(Usuario)
        .join(UsuarioRol, UsuarioRol.id_usuario == Usuario.id)
        .where(UsuarioRol.rol == "qf", Usuario.activo.is_(True))
    )
    usuarios_qf = db.execute(q).scalars().all()

    subject = f"Nuevo pedido #{numero or id_pedido} requiere revisión QF"
    link = f"{BASE_URL_ADMIN}/admin/pedidos/{id_pedido}"
    text = (
        f"Se ha creado el pedido #{numero or id_pedido} y requiere aprobación del Químico Farmacéutico.\n\n"
        f"Ver pedido: {link}"
    )
    html = f"<p>Se ha creado el pedido <strong>#{numero or id_pedido}</strong> y requiere aprobación del QF.</p><p><a href='{link}'>Ver pedido</a></p>"

    if not usuarios_qf:
        print(f"[MAIL][QF] No hay QF activos para notificar -> {subject}")
        return

    for u in usuarios_qf:
        correo = getattr(u, "correo", None) or getattr(u, "email", None)
        try:
            ok = False
            if correo:
                ok = send_email(to=correo, subject=subject, html=html, text=text)
            if not ok:
                # Modo dev o SMTP incompleto: deja rastro igual
                print(f"[MAIL][QF][DEV] TO={correo or u.usuario} subj='{subject}' -> {link}")
        except Exception as e:
            print(f"[MAIL][QF] Error notificando a {correo or u.usuario}: {e}")


# --------------------------------------------
# SQL para NOTAS del pedido (timeline)
# --------------------------------------------
SQL_PED_NOTAS_LIST = text("""
  SELECT id_nota, id_pedido, creado_en, autor_nombre, autor_rol,
         audiencia, destinatario_rol, texto
  FROM pedido_notas
  WHERE id_pedido = :id
  ORDER BY creado_en DESC
""")

SQL_PED_NOTA_INSERT = text("""
  INSERT INTO pedido_notas (id_pedido, autor_nombre, autor_rol, audiencia, destinatario_rol, texto)
  VALUES (:id_pedido, :autor_nombre, :autor_rol, :audiencia, :destinatario_rol, :texto)
  RETURNING id_nota
""")

@router.get("/{id_pedido}", response_class=HTMLResponse)
def admin_pedidos_detalle(
    id_pedido: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
):
    # Header
    header = db.execute(SQL_PEDIDO_HEADER, {"id": id_pedido}).mappings().first()
    if not header:
        return RedirectResponse(url="/admin/pedidos?err=not_found", status_code=303)

    # Items
    items = db.execute(SQL_PEDIDO_ITEMS, {"id": id_pedido}).mappings().all()

    # Notas (timeline) – tolerante si no existe la tabla
    try:
        notas = db.execute(SQL_PED_NOTAS_LIST, {"id": id_pedido}).mappings().all()
    except Exception as e:
        print(f"[pedidos] notas no disponibles: {e}")
        notas = []

    # Dirección de envío / facturación
    envio_dir = fetch_envio_direccion(
        db,
        header.get("id_direccion_envio"),
        header.get("id_cliente"),
    )
    fact_dir = envio_dir  # por ahora = envío

    # Próximos estados permitidos (desde la BD de transiciones)
    cur_code = header.get("estado_codigo")
    estados = _next_states_for(db, cur_code)
    print(f"[pedidos] estado actual={cur_code} siguientes={[e['codigo'] for e in estados]}")

    # Roles de flujo para el combo "Rol destinatario"
    roles_combo = _workflow_roles(db)

    # Rol por defecto = rol_responsable del primer estado siguiente (si existe)
    default_dest = None
    if estados:
        try:
            default_dest = estados[0].get("rol_responsable")
        except Exception:
            default_dest = None
    print(f"[pedidos] rol destinatario sugerido={default_dest}")

    # -------------------------------
    # NUEVO: datos de logística
    # -------------------------------

    # Transportista vigente (vista v_pedido_transportista_vigente) – tolerante si no existe
    carrier = {}
    try:
        carrier = db.execute(text("""
            SELECT transportista_nombre, tracking_ext, estado_logistico
            FROM public.v_pedido_transportista_vigente
            WHERE id_pedido = :id
            LIMIT 1
        """), {"id": id_pedido}).mappings().first() or {}
    except Exception as e:
        print(f"[pedidos] carrier no disponible: {e}")
        carrier = {}

    # Eventos logísticos (pedido_envio_eventos) – tolerante si no existe
    try:
        eventos_envio = db.execute(text("""
            SELECT id_evento, estado, nota, actor, actor_usuario, creado_en
            FROM public.pedido_envio_eventos
            WHERE id_pedido = :id
            ORDER BY creado_en DESC, id_evento DESC
        """), {"id": id_pedido}).mappings().all()
    except Exception as e:
        print(f"[pedidos] eventos_envio no disponibles: {e}")
        eventos_envio = []

    # Combo de transportistas activos para el modal “Asignar transportista”
    # (si la tabla no existe o falla, dejamos lista vacía para no romper la vista)
    try:
        transportistas_activos = db.execute(text("""
            SELECT id_transportista, nombre, rut
            FROM public.transportistas
            WHERE activo = TRUE
            ORDER BY nombre
        """)).mappings().all()
    except Exception as e:
        print(f"[pedidos] no fue posible cargar transportistas: {e}")
        transportistas_activos = []

    return templates.TemplateResponse(
        "admin_pedido_detalle.html",
        {
            "request": request,
            "header": header,
            "items": items,
            "envio_dir": envio_dir,
            "fact_dir": fact_dir,
            "admin_user": admin_user,
            "notas": notas,
            "estados": estados,              # solo los permitidos
            "roles_combo": roles_combo,      # opciones del combo
            "default_dest": default_dest,    # preselección del combo

            # 👇 NUEVO en el contexto
            "carrier": carrier,                              # transportista vigente/tracking/estado_logistico
            "eventos_envio": eventos_envio,                  # traza logística
            "transportistas_activos": transportistas_activos # para el modal de asignación
        },
    )

# === Helpers de dirección de envío ===
def _read_envio_from_form(form) -> dict:
    """Lee campos de dirección desde el form (soporta 'envio[foo]' o 'foo')."""
    def g(k: str) -> str:
        v = form.get(f"envio[{k}]") or form.get(k) or ""
        return v.strip()

    # id_cliente lo intentamos castear si viene
    try:
        id_cli = int(form.get("id_cliente")) if form.get("id_cliente") else None
    except Exception:
        id_cli = None

    return {
        "id_cliente":   id_cli,
        "nombre":       g("nombre") or None,
        "telefono":     g("telefono") or None,
        "calle":        g("calle") or None,
        "numero":       g("numero") or None,
        "depto":        g("depto") or None,
        "comuna":       g("comuna") or None,
        "ciudad":       g("ciudad") or None,
        "region":       g("region") or None,
        "referencia":   g("referencia") or None,
    }

# --- Roles de flujo tomados desde pedido_estados ---
ROLE_LABELS = {
    "QF": "Químico Farmacéutico",
    "PREPARACION": "Preparación",
    "LOGISTICA": "Logística",
    "ATENCION": "Atención",
    "TRANSPORTISTA": "Transportista",
}

SQL_WORKFLOW_ROLES = text("""
    SELECT DISTINCT rol_responsable
    FROM public.pedido_estados
    WHERE activo = TRUE
      AND NULLIF(TRIM(rol_responsable), '') IS NOT NULL
    ORDER BY rol_responsable
""")

def _workflow_roles(db):
    rows = db.execute(SQL_WORKFLOW_ROLES).scalars().all()
    # Mapea a etiqueta legible
    out = []
    for code in rows:
        code = (code or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": ROLE_LABELS.get(code, code.title().replace("_", " "))
        })
    return out

# ==============================
# POST: Crear nueva nota del pedido
# ==============================
@router.post("/{id_pedido}/notas/nueva")
def admin_pedidos_nota_nueva(
    id_pedido: int,
    request: Request,
    texto: str = Form(...),
    audiencia: str = Form("interno"),        # 'interno' o 'cliente'
    destinatario_rol: str = Form(""),        # opcional (bodega/ventas/reparto/cliente)
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_staff),
):
    autor_nombre = admin_user.get("nombre") or admin_user.get("username") or "admin"
    autor_rol = "admin"

    try:
        db.execute(SQL_PED_NOTA_INSERT, {
            "id_pedido": id_pedido,
            "autor_nombre": autor_nombre,
            "autor_rol": autor_rol,
            "audiencia": audiencia if audiencia in ("interno","cliente") else "interno",
            "destinatario_rol": destinatario_rol or None,
            "texto": (texto or "").strip(),
        })
        db.commit()
        return RedirectResponse(url=f"/admin/pedidos/{id_pedido}?ok=nota_creada", status_code=303)
    except Exception as e:
        db.rollback()
        print("[/admin/pedidos/notas/nueva] error:", repr(e))
        return RedirectResponse(url=f"/admin/pedidos/{id_pedido}?err=nota_error", status_code=303)