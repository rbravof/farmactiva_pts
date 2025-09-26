# app/routers/admin_pedidos.py
from __future__ import annotations
from app.utils.emailer import send_email
from fastapi import APIRouter, Depends, Request, Form, Query, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from app.models import Pedido, PedidoItem, PedidoNota, PedidoHistorial  # ORM
from starlette.datastructures import FormData
from app.database import get_db
from sqlalchemy.orm import Session
from datetime import datetime
import random, re
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from sqlalchemy import text
from typing import Optional, Dict
from sqlalchemy.sql import func
# Usa el mismo guard que en otros routers admin
from app.routers.admin_security import require_admin
from app.utils.view import render_admin

# === Pago ===
import os
try:
    import mercadopago  # type: ignore
except Exception:
    mercadopago = None  # type: ignore


templates = Jinja2Templates(directory="app/templates")

# ⚠️ IMPORTANTE: este 'router' es el que espera main.py
router = APIRouter(
    tags=["Admin Pedidos"],
    dependencies=[Depends(require_admin)]
)

@router.get("/admin/pedidos")
def admin_pedidos_list(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
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

@router.get("/admin/pedidos/nuevo")
def admin_pedidos_new(request: Request, admin_user: dict = Depends(require_admin)):
    return render_admin(templates, request, "admin_pedidos_form.html", {}, admin_user)

@router.post("/admin/pedidos/nuevo")
async def admin_pedidos_new_submit(
    request: Request,
    id_cliente: str = Form(None),
    canal: str = Form("manual"),
    accion: str = Form("solo_crear"),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
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

            envio = {
                "id_cliente":  id_cli,
                "nombre":      _get_env("nombre") or None,
                "telefono":    _get_env("telefono") or None,
                "calle":       _get_env("calle") or None,
                "numero":      _get_env("numero") or None,
                "depto":       _get_env("depto") or None,
                "comuna":      _get_env("comuna") or None,
                "ciudad":      _get_env("ciudad") or None,
                "region":      _get_env("region") or None,
                "referencia":  _get_env("referencia") or None,
            }

            # criterio mínimo para considerar que hay datos reales de dirección
            tiene_datos = any([envio["calle"], envio["comuna"], envio["ciudad"], envio["nombre"]])
            print(f"[PEDIDOS/NUEVO][{trc}] Datos dirección detectados: {envio} -> tiene_datos={tiene_datos}")

            if tiene_datos:
                res = db.execute(text("""
                    INSERT INTO public.direcciones_envio
                      (id_cliente, nombre, telefono, calle, numero, depto, comuna, ciudad, region, referencia)
                    VALUES
                      (:id_cliente, :nombre, :telefono, :calle, :numero, :depto, :comuna, :ciudad, :region, :referencia)
                    RETURNING id_direccion
                """), envio)
                id_direccion = res.scalar()
                print(f"[PEDIDOS/NUEVO][{trc}] Dirección creada id={id_direccion}")

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
            id_direccion_envio=id_direccion,  # <- ahora puede venir del insert en direcciones_envio
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

@router.get("/admin/pedidos/flujo")
def admin_pedidos_flujo(admin_user: dict = Depends(require_admin)):
    # Mermaid de ejemplo; luego se generará desde BD
    mermaid = """
    flowchart LR
      NUEVO["Nuevo (QF)"]
      APROBADO_QF["Aprobado QF (Preparación)"]
      APROBADO_CRITERIO_QF["Aprobado Criterio QF (Preparación)"]
      RECHAZADO_QF["Rechazado QF (Preparación)"]
      EN_PREPARACION["En Preparación (Logística)"]
      EN_TRANSITO["En Tránsito (Logística)"]
      ENTREGADO["Entregado (Atención)"]
      RECHAZADO_CLIENTE["Rechazado Cliente (Atención)"]

      NUEVO --> APROBADO_QF
      NUEVO --> APROBADO_CRITERIO_QF
      NUEVO --> RECHAZADO_QF

      APROBADO_QF --> EN_PREPARACION
      APROBADO_CRITERIO_QF --> EN_PREPARACION
      RECHAZADO_QF --> EN_PREPARACION

      EN_PREPARACION --> EN_TRANSITO
      EN_TRANSITO --> ENTREGADO
      EN_TRANSITO --> RECHAZADO_CLIENTE
    """.strip()
    return JSONResponse({"ok": True, "mermaid": mermaid})


@router.get("/admin/pedidos/{id_pedido}/siguientes-estados")
def admin_pedidos_siguientes_estados(id_pedido: int, admin_user: dict = Depends(require_admin)):
    # Stub: devolvemos opciones genéricas para que el modal funcione
    opciones = [
        {"codigo": "APROBADO_QF", "nombre": "Aprobado QF", "rol_destino": "PREPARACION"},
        {"codigo": "APROBADO_CRITERIO_QF", "nombre": "Aprobado Bajo Criterio QF", "rol_destino": "PREPARACION"},
        {"codigo": "RECHAZADO_QF", "nombre": "Rechazado QF", "rol_destino": "PREPARACION"},
    ]
    return JSONResponse({"ok": True, "opciones": opciones})

@router.post("/admin/pedidos/{id_pedido}/cambiar-estado")
def admin_pedidos_cambiar_estado(
    id_pedido: int,
    nuevo_estado: str = Form(...),
    nota_cliente: str | None = Form(None),
    nota_rol: str | None = Form(None),
    destinatario_rol: str | None = Form(None),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    print(f"[pedidos/actions] cambiar estado id={id_pedido} -> {nuevo_estado}")

    # Sanitiza
    def _clean(s):
        return s.strip() if isinstance(s, str) and s.strip() else None
    nota_cliente = _clean(nota_cliente)
    nota_rol = _clean(nota_rol)
    destinatario_rol = _clean(destinatario_rol)

    if not nuevo_estado or len(nuevo_estado) > 64:
        return JSONResponse({"ok": False, "error": "estado inválido"})

    # Estado actual
    cur = db.execute(text("SELECT estado_codigo FROM public.pedidos WHERE id_pedido = :id"),
                     {"id": id_pedido}).scalar()

    # Validación de transición (si hay reglas)
    allowed = _next_states_for(db, cur)
    allowed_codes = {e["codigo"] for e in allowed}
    if allowed_codes and nuevo_estado not in allowed_codes:
        return JSONResponse({"ok": False, "error": "Transición no permitida"})

    # Validación opcional de catálogo
    try:
        exists = db.execute(
            text("SELECT 1 FROM public.pedido_estados WHERE codigo = :c LIMIT 1"),
            {"c": nuevo_estado}
        ).scalar()
        if not exists:
            print("[pedidos/actions] WARNING: estado no está en catálogo; se continúa igual")
    except Exception:
        pass

    # Actualizar estado
    db.execute(text("""
        UPDATE public.pedidos
           SET estado_codigo = :estado
         WHERE id_pedido = :id
    """), {"estado": nuevo_estado, "id": id_pedido})

    # Registrar notas (si la tabla existe)
    autor = (admin_user or {}).get("nombre") or "admin"
    try:
        # Nota automática del cambio
        db.execute(text("""
            INSERT INTO public.pedido_notas (id_pedido, texto, audiencia, creado_en, autor_nombre, autor_rol)
            VALUES (:id, :texto, 'interno', now(), :autor, 'admin')
        """), {"id": id_pedido, "texto": f"Estado cambiado de {cur or '—'} a {nuevo_estado}", "autor": autor})

        # Nota para el cliente (visible al cliente)
        if nota_cliente:
            db.execute(text("""
                INSERT INTO public.pedido_notas (id_pedido, texto, audiencia, creado_en, autor_nombre, autor_rol)
                VALUES (:id, :texto, 'cliente', now(), :autor, 'admin')
            """), {"id": id_pedido, "texto": nota_cliente, "autor": autor})

        # Nota interna para el próximo rol
        if nota_rol or destinatario_rol:
            db.execute(text("""
                INSERT INTO public.pedido_notas (id_pedido, texto, audiencia, destinatario_rol, creado_en, autor_nombre, autor_rol)
                VALUES (:id, :texto, 'interno', :destinatario, now(), :autor, 'admin')
            """), {
                "id": id_pedido,
                "texto": nota_rol or ("Instrucciones para " + destinatario_rol if destinatario_rol else "Instrucciones"),
                "destinatario": destinatario_rol,
                "autor": autor,
            })
    except Exception as e:
        print(f"[pedidos/actions] notas opcionales omitidas: {e}")

    db.commit()
    print("[pedidos/actions] cambio de estado OK con notas adicionales")
    return JSONResponse({"ok": True, "nuevo_estado": nuevo_estado})

# ============================
# Productos: búsqueda y precio
# ============================

@router.get("/admin/productos/buscar")
def admin_productos_buscar(
    q: str,
    id_lista: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    q = (q or "").strip()
    if len(q) < 2:
        return []

    params = {
        "q_name": f"%{q.lower()}%",
        "q_ean":  f"%{q}%",
        "limit":  max(1, min(limit, 50)),
    }
    id_lista_filter = ""
    if id_lista is not None:
        id_lista_filter = "AND pr.id_lista = :id_lista"
        params["id_lista"] = id_lista

    sql = f"""
        SELECT
          p.id_producto                   AS id,
          p.titulo                        AS nombre,
          p.slug                          AS slug,
          p.imagen_principal_url          AS imagen,
          (
            SELECT cb.codigo_barra
            FROM public.codigos_barras cb
            WHERE cb.id_producto = p.id_producto AND cb.es_principal = TRUE
            LIMIT 1
          ) AS ean,
          prx.precio_sugerido
        FROM public.productos p
        LEFT JOIN LATERAL (
          SELECT CAST(ROUND(pr.precio_bruto) AS INTEGER) AS precio_sugerido
          FROM public.precios pr
          WHERE pr.id_producto = p.id_producto
            {id_lista_filter}
            AND (pr.vigente_hasta IS NULL OR pr.vigente_hasta >= now())
          ORDER BY pr.vigente_desde DESC, pr.id_precio DESC
          LIMIT 1
        ) prx ON TRUE
        WHERE
              LOWER(p.titulo) LIKE :q_name
          OR  LOWER(p.slug)   LIKE :q_name
          OR  EXISTS (
                SELECT 1
                FROM public.codigos_barras cb2
                WHERE cb2.id_producto = p.id_producto
                  AND cb2.codigo_barra ILIKE :q_ean
              )
        ORDER BY LOWER(p.titulo) ASC
        LIMIT :limit
    """

    rows = db.execute(text(sql), params).mappings().all()
    items = [{
        "id": r["id"],
        "nombre": r["nombre"],
        "slug": r["slug"],
        "imagen": r["imagen"],
        "ean": r["ean"],
        "precio_sugerido": int(r["precio_sugerido"]) if r["precio_sugerido"] is not None else 0,
    } for r in rows]

    print(f"[BUSCAR productos] q='{q}' -> {len(items)} coincidencias")
    return items


@router.get("/admin/productos/precio")
def admin_productos_precio(
    id_producto: int,
    id_lista: int | None = None,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    try:
        params = {"id": id_producto}
        id_lista_filter = ""
        if id_lista is not None:
            id_lista_filter = "AND pr.id_lista = :id_lista"
            params["id_lista"] = id_lista

        sql = f"""
            SELECT CAST(ROUND(pr.precio_bruto) AS INTEGER) AS precio
            FROM public.precios pr
            WHERE pr.id_producto = :id
              {id_lista_filter}
              AND (pr.vigente_hasta IS NULL OR pr.vigente_hasta >= now())
            ORDER BY pr.vigente_desde DESC, pr.id_precio DESC
            LIMIT 1
        """

        precio = db.execute(text(sql), params).scalar()
        return JSONResponse({"ok": True, "precio": int(precio or 0)})
    except Exception as e:
        print("[/admin/productos/precio] error:", e)
        return JSONResponse({"ok": False, "precio": 0})


# =======================================
# Envíos: tipos y cálculo dinámico tarifa
# =======================================

# 3.1) Listado de tipos de envío activos (para poblar el <select>)
@router.get("/admin/api/envios/tipos")
def api_envios_tipos(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id_tipo_envio AS id, codigo, nombre, requiere_direccion
        FROM public.tipos_envio
        WHERE activo = TRUE
        ORDER BY orden ASC, nombre ASC
    """)).mappings().all()
    # devolvemos lista simple para que el HTML pueda iterarla
    return {"ok": True, "items": rows}


# 3.2) Cálculo de costo de envío con reglas por comuna / región / default
@router.get("/admin/api/envios/tarifa")
def api_envios_tarifa(
    id_tipo_envio: int = Query(...),
    id_comuna: int | None = Query(None),
    id_region: int | None = Query(None),
    subtotal_items: int = Query(0),             # total de ítems (CLP, sin IVA si así decides)
    peso_total_g: int | None = Query(None),     # opcional si manejas pesos
    db: Session = Depends(get_db),
):
    """
    Selecciona la mejor regla:
      1) match por comuna (activo)
      2) match por región (activo)
      3) regla 'por defecto' (sin comuna/ región)
    respetando prioridad (menor = más específica).
    Aplica gratis_desde si corresponde.
    """
    params = {
        "id_tipo": id_tipo_envio,
        "id_comuna": id_comuna,
        "id_region": id_region,
        "peso": peso_total_g,
    }

    sql = """
    WITH cand AS (
      SELECT
        t.id_tarifa, t.base_clp, t.gratis_desde, t.prioridad,
        CASE WHEN t.id_comuna IS NOT NULL THEN 1
             WHEN t.id_region IS NOT NULL THEN 2
             ELSE 3 END AS nivel
      FROM public.envio_tarifas t
      WHERE t.id_tipo_envio = :id_tipo
        AND t.activo = TRUE
        AND COALESCE(:peso, 0) >= COALESCE(t.peso_min_g, 0)
        AND (t.peso_max_g IS NULL OR COALESCE(:peso, 0) <= t.peso_max_g)
        AND (
              (:id_comuna IS NOT NULL AND t.id_comuna = :id_comuna)
           OR (:id_comuna IS NULL  AND :id_region IS NOT NULL AND t.id_region = :id_region)
           OR (t.id_comuna IS NULL AND t.id_region IS NULL)
        )
    )
    SELECT base_clp, gratis_desde
    FROM cand
    ORDER BY nivel ASC, prioridad ASC
    LIMIT 1;
    """
    row = db.execute(text(sql), params).mappings().first()
    if not row:
        return {"ok": True, "costo": 0, "motivo": "sin_regla"}

    costo = int(row["base_clp"] or 0)
    if row["gratis_desde"] is not None and subtotal_items >= int(row["gratis_desde"]):
        costo = 0
    return {"ok": True, "costo": costo}


# Variante interna usada por el paso 2 (compatibilidad con tu HTML actual)
@router.get("/admin/envios/tarifa")
def admin_envios_tarifa(
    id_tipo_envio: int = Query(..., alias="id_tipo_envio"),
    id_region: Optional[int] = Query(None),
    id_comuna: Optional[int] = Query(None),
    subtotal: int = Query(0),  # subtotal neto de ítems (sin IVA)
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    rows = db.execute(text("""
        SELECT
            t.id_tarifa, t.base_clp, t.gratis_desde, t.prioridad,
            t.id_region, t.id_comuna
        FROM public.envio_tarifas t
        WHERE t.activo = TRUE
          AND t.id_tipo_envio = :tipo
          AND (:id_comuna IS NULL OR t.id_comuna IS NULL OR t.id_comuna = :id_comuna)
          AND (:id_region IS NULL OR t.id_region IS NULL OR t.id_region = :id_region)
        ORDER BY
          CASE WHEN t.id_comuna IS NOT NULL THEN 0
               WHEN t.id_region IS NOT NULL THEN 1
               ELSE 2 END,
          t.prioridad ASC, t.base_clp ASC
        LIMIT 1
    """), {
        "tipo": id_tipo_envio,
        "id_region": id_region,
        "id_comuna": id_comuna,
    }).mappings().all()

    if not rows:
        return {"ok": True, "costo": 0, "aplicado_gratis": False}

    t = rows[0]
    costo = int(t["base_clp"] or 0)
    aplicado_gratis = False
    if t["gratis_desde"] is not None and subtotal >= int(t["gratis_desde"]):
        costo = 0
        aplicado_gratis = True

    return {
        "ok": True,
        "costo": costo,
        "aplicado_gratis": aplicado_gratis,
        "id_tarifa": t["id_tarifa"],
    }

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

@router.post("/admin/api/pagos/mp/preferencias")
def api_mp_crear_preferencia(
    # datos mínimos; el front calcula totales
    titulo: str = Form("Pedido Farmactiva"),
    descripcion: str = Form(""),
    total_clp: int = Form(...),                   # total final en CLP (con IVA y envío)
    email_cliente: str = Form(""),
    external_reference: str = Form(""),
    # URLs de retorno (opcional; por defecto volvemos al admin):
    success_url: str = Form("/admin/pedidos"),
    failure_url: str = Form("/admin/pedidos"),
    pending_url: str = Form("/admin/pedidos"),
    admin_user: dict = Depends(require_admin),
):
    sdk, err = _get_mp_client()
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=500)

    unit_price = float(int(total_clp or 0))  # MP usa float, CLP sin decimales

    preference = {
        "items": [{
            "title": titulo or "Pedido Farmactiva",
            "description": descripcion or "",
            "quantity": 1,
            "currency_id": "CLP",
            "unit_price": unit_price,
        }],
        "auto_return": "approved",
        "back_urls": {
            "success": success_url,
            "failure": failure_url,
            "pending": pending_url,
        },
        "external_reference": external_reference or "",
        "payer": {"email": email_cliente} if email_cliente else {},
        # "notification_url": "https://TU_DOMINIO/mercadopago/webhook"  # si ya tienes público
    }

    try:
        pref = sdk.preference().create(preference)
        body = pref.get("response", {})
        return {
            "ok": True,
            "id": body.get("id"),
            "init_point": body.get("init_point"),
            "sandbox_init_point": body.get("sandbox_init_point"),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/pagos/mercadopago/callback")
def mp_callback_collector(
    status: str = Query(None),
    payment_id: str = Query(None, alias="payment_id"),
    preference_id: str = Query(None, alias="preference_id"),
    external_reference: str = Query(None),
    admin_user: dict = Depends(require_admin),
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
@router.get("/admin/pedidos/{id_pedido}/etiqueta", response_class=HTMLResponse)
def admin_pedido_etiqueta(
    id_pedido: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
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

def _next_states_for(db: Session, current_code: Optional[str]) -> list[dict]:
    """
    Devuelve [{codigo, nombre}] con los estados permitidos *siguientes* al estado actual.
    Reglas de negocio:
      - NUEVO -> APROBADO_QF | APROBADO_CRITERIO_QF | RECHAZADO_QF
      - APROBADO_QF -> PREPARANDO
      - APROBADO_CRITERIO_QF -> PREPARANDO
      - PREPARANDO -> ENVIADO
      - ENVIADO -> ENTREGADO
    Si existen columnas next_codes/siguiente/orden en 'pedido_estados', se usan por encima
    (para no romper catálogos ya definidos).
    """
    cur = (current_code or "").strip()
    cur_upper = cur.upper()

    # 0) Reglas explícitas
    explicit = {
        "NUEVO": ["APROBADO_QF", "APROBADO_CRITERIO_QF", "RECHAZADO_QF"],
        "APROBADO_QF": ["PREPARANDO"],
        "APROBADO_CRITERIO_QF": ["PREPARANDO"],
        "PREPARANDO": ["ENVIADO"],
        "ENVIADO": ["ENTREGADO"],
    }
    if cur_upper in explicit:
        desired = explicit[cur_upper]
    else:
        desired = []

    # 1) Intentar respetar catálogo si tiene transiciones declaradas
    try:
        cols = db.execute(text("""
            SELECT lower(column_name)
              FROM information_schema.columns
             WHERE table_schema='public' AND table_name='pedido_estados'
        """)).scalars().all()
        cols = set(cols)
    except Exception:
        cols = set()

    def _csv_to_codes(s: Optional[str]) -> list[str]:
        return [x.strip() for x in (s or "").split(",") if x.strip()]

    # next_codes o siguiente (CSV) tienen prioridad si existen
    for col in ("next_codes", "siguiente"):
        if col in cols:
            try:
                row = db.execute(
                    text(f"SELECT {col} FROM public.pedido_estados WHERE upper(codigo)=upper(:c) LIMIT 1"),
                    {"c": cur}
                ).mappings().first()
                cand = _csv_to_codes(row[col]) if row and row.get(col) else []
                if cand:
                    desired = cand  # catálogo manda
            except Exception:
                pass

    # orden: si existe y no hay desired explícito, usar inmediato siguiente
    if not desired and "orden" in cols:
        try:
            cur_ord = db.execute(
                text("SELECT orden FROM public.pedido_estados WHERE upper(codigo)=upper(:c) LIMIT 1"),
                {"c": cur},
            ).scalar()
            if cur_ord is not None:
                rows = db.execute(text("""
                    SELECT codigo, COALESCE(NULLIF(nombre,''), codigo) AS nombre
                      FROM public.pedido_estados
                     WHERE orden > :o
                     ORDER BY orden ASC
                     LIMIT 1
                """), {"o": cur_ord}).mappings().all()
                return [dict(r) for r in rows]
        except Exception:
            pass

    # Materializar salida (con nombres del catálogo si están)
    out: list[dict] = []
    for code in desired:
        try:
            r = db.execute(text("""
                SELECT codigo, COALESCE(NULLIF(nombre,''), codigo) AS nombre
                  FROM public.pedido_estados
                 WHERE upper(codigo) = upper(:c)
                 LIMIT 1
            """), {"c": code}).mappings().first()
            out.append(dict(r) if r else {"codigo": code, "nombre": code})
        except Exception:
            out.append({"codigo": code, "nombre": code})
    return out

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

@router.get("/admin/pedidos/{id_pedido}")
def admin_pedidos_detalle(
    id_pedido: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
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

    # Dirección de envío
    envio_dir = fetch_envio_direccion(
        db,
        header.get("id_direccion_envio"),
        header.get("id_cliente")
    )
    fact_dir = envio_dir  # por ahora = envío

    # >>> SOLO SIGUIENTES ESTADOS <<<
    estados = _next_states_for(db, header.get("estado_codigo"))
    print(f"[pedidos] estado actual={header.get('estado_codigo')} siguientes={ [e['codigo'] for e in estados] }")

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
            "estados": estados,  # el modal ahora verá solo los permitidos
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


# ==============================
# POST: Crear nueva nota del pedido
# ==============================
@router.post("/admin/pedidos/{id_pedido}/notas/nueva")
def admin_pedidos_nota_nueva(
    id_pedido: int,
    request: Request,
    texto: str = Form(...),
    audiencia: str = Form("interno"),        # 'interno' o 'cliente'
    destinatario_rol: str = Form(""),        # opcional (bodega/ventas/reparto/cliente)
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
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