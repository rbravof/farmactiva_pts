# app/routers/admin_pagos.py
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.routers.admin_security import require_admin
from app.database import get_db

router = APIRouter()

# ========== SQL helpers ==========
SQL_NOTA_INSERT = text("""
  INSERT INTO public.pedido_notas (id_pedido, autor_nombre, autor_rol, audiencia, destinatario_rol, texto, creado_en)
  VALUES (:id_pedido, :autor_nombre, :autor_rol, :audiencia, :destinatario_rol, :texto, now())
""")

SQL_PEDIDO_UPDATE_PAGO = text("""
  UPDATE public.pedidos SET
    pago_estado = :pago_estado,
    pago_proveedor = NULLIF(:pago_proveedor, '')::text,
    pago_monto = :pago_monto,
    pago_moneda = :pago_moneda
  WHERE id_pedido = :id_pedido
""")

SQL_PPAGO_INSERT = text("""
  INSERT INTO public.pedido_pagos (id_pedido, proveedor, link_url, monto, moneda, estado)
  VALUES (:id_pedido, :proveedor, :link_url, :monto, :moneda, :estado)
  RETURNING id_pago
""")

SQL_PPAGO_INSERT_PAGADO = text("""
  INSERT INTO public.pedido_pagos (id_pedido, proveedor, link_url, monto, moneda, estado, pagado_en)
  VALUES (:id_pedido, :proveedor, :link_url, :monto, :moneda, 'pagado', now())
  RETURNING id_pago
""")

SQL_PPAGO_SELECT_PEND = text("""
  SELECT id_pago
    FROM public.pedido_pagos
   WHERE id_pedido = :id_pedido
     AND estado = 'pendiente'
   ORDER BY creado_en DESC
   LIMIT 1
""")

SQL_PPAGO_MARK_PAGADO = text("""
  UPDATE public.pedido_pagos
     SET estado = 'pagado',
         pagado_en = now(),
         proveedor = COALESCE(NULLIF(:proveedor,''), proveedor),
         link_url = COALESCE(NULLIF(:link_url,''), link_url),
         monto = :monto,
         moneda = :moneda
   WHERE id_pago = :id_pago
""")

# ========== Endpoints ==========

@router.post("/admin/pagos/{id_pedido}/enviar-solicitud")
def admin_pagos_enviar_solicitud(
    id_pedido: int,
    email_to: str = Form(...),
    monto: int = Form(...),
    moneda: str = Form("CLP"),
    mensaje: str = Form(""),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    """
    Flujo robusto con commits intermedios:
      (A) INSERT pendiente -> COMMIT
      (B) Crear preferencia MP y UPDATE link_url -> COMMIT
      (C) Enviar correo y registrar nota -> COMMIT (pase lo que pase el link queda)
    """
    from app.utils.emailer import send_email
    from app.services.mercadopago import create_mp_preference
    from sqlalchemy import text

    def _fmt_num(n: str | None) -> str:
        if not n:
            try: return f"#{1000 + int(id_pedido)}"
            except Exception: return f"#{id_pedido}"
        s = str(n).strip()
        return s if s.startswith("#") else f"#{s}"

    # === 0) número del pedido
    numero = db.execute(text("SELECT numero FROM public.pedidos WHERE id_pedido=:id LIMIT 1"),
                        {"id": id_pedido}).scalar()
    numero_fmt = _fmt_num(numero)

    autor = (admin_user or {}).get("nombre") or "admin"
    print(f"[pagos/email] PREPARE -> pedido_id={id_pedido} numero={numero_fmt} to={email_to} monto={monto} {moneda}")

    # === (A) insertar pendiente y COMMIT
    try:
        id_pago = db.execute(SQL_PPAGO_INSERT, {
            "id_pedido": id_pedido,
            "proveedor": "MercadoPago",
            "link_url": None,
            "monto": monto,
            "moneda": moneda,
            "estado": "pendiente",
        }).scalar_one()
        db.commit()
        print(f"[pagos/email] pedido_pagos INSERT pendiente -> id_pago={id_pago}")
    except Exception as e:
        db.rollback()
        print("[pagos/email] ERROR insert pedido_pagos pendiente:", e)
        return {"ok": False, "error": f"DB insert pedido_pagos: {e}"}

    # === (B) crear preferencia y actualizar link_url; COMMIT
    try:
        pref = create_mp_preference(
            id_pago=id_pago,
            id_pedido=id_pedido,
            numero_fmt=numero_fmt,
            monto=monto,
            moneda=moneda or "CLP",
            email_to=email_to,
        )
        link_url = pref.get("init_point")
        if not link_url:
            raise RuntimeError("MercadoPago no devolvió init_point")
        db.execute(text("UPDATE public.pedido_pagos SET link_url=:u WHERE id_pago=:id"),
                   {"u": link_url, "id": id_pago})
        db.commit()
        print(f"[pagos/mp] UPDATE pedido_pagos.link_url={link_url}")
    except Exception as e:
        db.rollback()
        print("[pagos/mp] ERROR creando preferencia / actualizando link_url:", e)
        # OJO: dejamos la fila pendiente creada; puedes regenerar link con el endpoint de abajo
        return {"ok": False, "error": f"MercadoPago: {e}", "id_pago": id_pago}

    # === (C) enviar correo (si falla, igual conservamos link en DB) y registrar nota; COMMIT
    asunto = f"Solicitud de pago pedido {numero_fmt}"
    html = f"""
      <h2>Pago pendiente</h2>
      <p>Hola, te enviamos la solicitud de pago por <strong>{monto} {moneda}</strong> del pedido <strong>{numero_fmt}</strong>.</p>
      <p><a href="{link_url}" target="_blank" rel="noopener" style="display:inline-block;padding:10px 14px;background:#047857;color:#fff;border-radius:8px;text-decoration:none;">Pagar ahora</a></p>
      {f'<p>{mensaje}</p>' if mensaje else ''}
    """.strip()
    text_alt = f"Pago pendiente por {monto} {moneda} del pedido {numero_fmt}.\nPagar ahora: {link_url}"
    if mensaje:
        text_alt += f"\n{mensaje}"

    ok = False
    try:
        ok = send_email(email_to, asunto, html, text_alt)
        print(f"[pagos/email] send_email resp={ok}")
    except Exception as e:
        print("[pagos/email] EXC send_email:", e)

    nota = f"Se envió solicitud de pago por {monto} {moneda} del pedido {numero_fmt} a {email_to}."
    nota += " ✅ Enviado" if ok else " ❌ Error al enviar"
    try:
        db.execute(SQL_NOTA_INSERT, {
            "id_pedido": id_pedido,
            "autor_nombre": autor,
            "autor_rol": "admin",
            "audiencia": "cliente",
            "destinatario_rol": None,
            "texto": nota,
        })
        db.commit()
        print("[pagos/email] NOTA registrada (cliente).")
    except Exception as e:
        db.rollback()
        print("[pagos/email] ERROR commit/nota:", e)
        return {"ok": ok, "error": f"DB commit: {e}", "id_pago": id_pago, "link_url": link_url}

    return {"ok": ok, "id_pago": id_pago, "link_url": link_url, "numero": numero_fmt}

@router.post("/admin/pagos/{id_pedido}/regenerar-link")
def admin_pagos_regenerar_link(
    id_pedido: int,
    email_to: str | None = Form(None),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    """
    Busca el último pago 'pendiente' SIN link_url y genera una preferencia MP.
    Si 'email_to' viene, reenvía el correo con el link.
    """
    from app.utils.emailer import send_email
    from app.services.mercadopago import create_mp_preference
    from sqlalchemy import text

    def _fmt_num(n: str | None) -> str:
        if not n:
            try: return f"#{1000 + int(id_pedido)}"
            except Exception: return f"#{id_pedido}"
        s = str(n).strip()
        return s if s.startswith("#") else f"#{s}"

    numero = db.execute(text("SELECT numero FROM public.pedidos WHERE id_pedido=:id LIMIT 1"),
                        {"id": id_pedido}).scalar()
    numero_fmt = _fmt_num(numero)

    row = db.execute(text("""
        SELECT id_pago, monto, moneda, COALESCE(link_url, '') AS link_url
          FROM public.pedido_pagos
         WHERE id_pedido = :id AND estado = 'pendiente'
         ORDER BY id_pago DESC
         LIMIT 1
    """), {"id": id_pedido}).mappings().first()

    if not row:
        return {"ok": False, "error": "no_pending_payment"}

    id_pago = row["id_pago"]; monto = int(row["monto"]); moneda = row["moneda"]; link_url = row["link_url"] or None

    if link_url:
        print(f"[pagos/mp] Ya existe link_url para id_pago={id_pago}: {link_url}")
        return {"ok": True, "id_pago": id_pago, "link_url": link_url, "numero": numero_fmt}

    # crear preferencia y actualizar
    try:
        pref = create_mp_preference(
            id_pago=id_pago,
            id_pedido=id_pedido,
            numero_fmt=numero_fmt,
            monto=monto,
            moneda=moneda,
            email_to=email_to,
        )
        link_url = pref.get("init_point")
        if not link_url:
            raise RuntimeError("MercadoPago no devolvió init_point")
        db.execute(text("UPDATE public.pedido_pagos SET link_url=:u WHERE id_pago=:id"),
                   {"u": link_url, "id": id_pago})
        db.commit()
        print(f"[pagos/mp] REGEN link_url OK id_pago={id_pago} -> {link_url}")
    except Exception as e:
        db.rollback()
        print("[pagos/mp] ERROR regenerando link_url:", e)
        return {"ok": False, "error": str(e)}

    # reenvío de correo (opcional)
    if email_to:
        asunto = f"Solicitud de pago pedido {numero_fmt}"
        html = f"""
          <h2>Pago pendiente</h2>
          <p>Enlace de pago del pedido <strong>{numero_fmt}</strong>: </p>
          <p><a href="{link_url}" target="_blank" rel="noopener" style="display:inline-block;padding:10px 14px;background:#047857;color:#fff;border-radius:8px;text-decoration:none;">Pagar ahora</a></p>
        """.strip()
        text_alt = f"Pagar ahora: {link_url}"
        try:
            ok = send_email(email_to, asunto, html, text_alt)
            print(f"[pagos/email] REENVIO resp={ok}")
        except Exception as e:
            print("[pagos/email] REENVIO EXC:", e)

    return {"ok": True, "id_pago": id_pago, "link_url": link_url, "numero": numero_fmt}

@router.post("/admin/pagos/{id_pedido}/marcar-pagado")
def admin_pagos_marcar_pagado(
    id_pedido: int,
    forma_pago: str = Form(...),   # tarjeta|transferencia|efectivo|otro
    medio_pago: str = Form(...),   # Webpay|MercadoPago|Transferencia|Caja/Local|Otro
    proveedor: str = Form(""),     # opcional (p. ej. "Transbank")
    monto: int = Form(...),
    moneda: str = Form("CLP"),
    ref_transaccion: str = Form(""),
    nota_cliente: str = Form(""),
    nota_interna: str = Form(""),
    destinatario_rol: str | None = Form(None),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(require_admin),
):
    """
    Marca el pedido como pagado:
      - Actualiza 'pedidos' (pago_estado/monto/moneda/proveedor)
      - Busca un pedido_pagos pendiente -> lo marca 'pagado' (o inserta uno 'pagado' si no existe)
      - Inserta notas opcionales
    """
    prov_final = (proveedor or medio_pago or "manual").strip()
    print(f"[pagos] Marcar pagado -> pedido={id_pedido} forma={forma_pago} medio={medio_pago} prov={prov_final} monto={monto} {moneda} ref={ref_transaccion}")

    try:
        # 1) actualizar cabecera de pago en pedidos
        db.execute(SQL_PEDIDO_UPDATE_PAGO, {
            "id_pedido": id_pedido,
            "pago_estado": "pagado",
            "pago_proveedor": prov_final,
            "pago_monto": monto,
            "pago_moneda": moneda,
        })

        # 2) actualizar/insertar pedido_pagos
        id_pago = db.execute(SQL_PPAGO_SELECT_PEND, {"id_pedido": id_pedido}).scalar()
        if id_pago:
            db.execute(SQL_PPAGO_MARK_PAGADO, {
                "id_pago": id_pago,
                "proveedor": prov_final,
                "link_url": None,  # no link en pago manual
                "monto": monto,
                "moneda": moneda,
            })
            print(f"[pagos] pedido_pagos #{id_pago} -> pagado")
        else:
            id_pago = db.execute(SQL_PPAGO_INSERT_PAGADO, {
                "id_pedido": id_pedido,
                "proveedor": prov_final,
                "link_url": None,
                "monto": monto,
                "moneda": moneda,
            }).scalar_one()
            print(f"[pagos] pedido_pagos creado (pagado) id_pago={id_pago}")

        # 3) notas opcionales
        autor = (admin_user or {}).get("nombre") or "admin"
        if nota_cliente.strip():
            db.execute(SQL_NOTA_INSERT, {
                "id_pedido": id_pedido, "autor_nombre": autor,
                "autor_rol": "admin", "audiencia": "cliente",
                "destinatario_rol": None,
                "texto": nota_cliente.strip()
            })
        if nota_interna.strip() or (destinatario_rol and destinatario_rol.strip()):
            body = f"[Pago manual {forma_pago}/{medio_pago} ref={ref_transaccion}]"
            if nota_interna.strip():
                body += f" {nota_interna.strip()}"
            db.execute(SQL_NOTA_INSERT, {
                "id_pedido": id_pedido, "autor_nombre": autor,
                "autor_rol": "admin", "audiencia": "interno",
                "destinatario_rol": (destinatario_rol or None),
                "texto": body,
            })

        db.commit()
        return {"ok": True, "id_pago": id_pago}
    except Exception as e:
        db.rollback()
        print("[pagos] ERROR marcando pagado:", e)
        return {"ok": False, "error": str(e)}

# Webhook Mercado Pago
@router.post("/integrations/mercadopago/webhook")
async def mp_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Webhook para notificaciones de Mercado Pago.
    Maneja 'type=payment' (nuevo formato) o 'topic=payment' (antiguo).
    Consulta el pago y registra evento; si está aprobado, marca el pedido como pagado.
    """
    import json
    from sqlalchemy import text

    # --- leer body + querystring
    try:
        body = await request.json()
    except Exception:
        body = {}
    qs = dict(request.query_params)
    print(f"[pagos/mp] WEBHOOK qs={qs} body={body}")

    # --- identificar payment_id
    payment_id = body.get("data", {}).get("id") or qs.get("id") or qs.get("data.id")
    topic = (body.get("type") or qs.get("type") or qs.get("topic") or "").lower()
    if not payment_id or (topic and topic != "payment"):
        print("[pagos/mp] Ignorado: no es payment")
        return {"ok": True}

    # --- consultar MP
    from app.services.mercadopago import get_mp_payment
    try:
        p = get_mp_payment(payment_id)
    except Exception as e:
        print("[pagos/mp] ERROR get_mp_payment:", e)
        return {"ok": False}

    # --- extraer campos clave
    status = (p.get("status") or "").lower()            # approved/pending/rejected/...
    status_detail = p.get("status_detail")              # detalle granular MP
    ext_ref = p.get("external_reference")               # id_pago (lo pusimos al crear preferencia)
    tr_amount = int(round(float(p.get("transaction_amount") or 0)))
    currency = p.get("currency_id") or "CLP"
    mp_payment_id = str(p.get("id") or payment_id)

    print(f"[pagos/mp] payment_id={mp_payment_id} status={status} detail={status_detail} external_ref={ext_ref} amount={tr_amount} {currency}")

    if not ext_ref:
        print("[pagos/mp] Sin external_reference -> no puedo mapear id_pago")
        return {"ok": True}

    # --- mapear id_pago
    try:
        id_pago = int(ext_ref)
    except Exception:
        print(f"[pagos/mp] external_reference no convertible a int: {ext_ref!r}")
        return {"ok": True}

    # --- registrar evento (histórico)
    try:
        db.execute(text("""
            INSERT INTO public.pedido_pagos_eventos
                (id_pago, tipo, estado, estado_detalle, proveedor_payment_id, payload)
            VALUES
                (:id_pago, :tipo, :estado, :estado_detalle, :prov_id, :payload::jsonb)
        """), {
            "id_pago": id_pago,
            "tipo": "webhook",
            "estado": status,
            "estado_detalle": status_detail,
            "prov_id": mp_payment_id,
            "payload": json.dumps(p, ensure_ascii=False),
        })
        # También reflejar referencias en la tabla principal
        db.execute(text("""
            UPDATE public.pedido_pagos
               SET proveedor_payment_id = :prov_id,
                   estado_detalle       = :estado_detalle
             WHERE id_pago = :id_pago
        """), {"prov_id": mp_payment_id, "estado_detalle": status_detail, "id_pago": id_pago})
        db.commit()
    except Exception as e:
        db.rollback()
        print("[pagos/mp] WARN registrando evento:", e)
        # seguimos, no abortamos el webhook por problemas de auditoría

    # --- marcar pagado / o actualizar estado no-aprobado
    try:
        # obtener id_pedido desde la fila
        row = db.execute(text("SELECT id_pedido FROM public.pedido_pagos WHERE id_pago=:id"),
                         {"id": id_pago}).mappings().first()
        if not row:
            print(f"[pagos/mp] No existe pedido_pagos #{id_pago}")
            return {"ok": True}
        id_pedido = row["id_pedido"]

        if status == "approved":
            # actualizar intento + cabecera
            db.execute(text("""
                UPDATE public.pedido_pagos
                   SET estado='pagado',
                       pagado_en=now(),
                       monto=:monto,
                       moneda=:moneda
                 WHERE id_pago=:id_pago
            """), {"id_pago": id_pago, "monto": tr_amount, "moneda": currency})

            db.execute(text("""
                UPDATE public.pedidos
                   SET pago_estado='pagado',
                       pago_proveedor='MercadoPago',
                       pago_monto=:monto,
                       pago_moneda=:moneda
                 WHERE id_pedido=:id_pedido
            """), {"id_pedido": id_pedido, "monto": tr_amount, "moneda": currency})

            db.execute(SQL_NOTA_INSERT, {
                "id_pedido": id_pedido,
                "autor_nombre": "webhook",
                "autor_rol": "sistema",
                "audiencia": "interno",
                "destinatario_rol": None,
                "texto": f"[MP] Pago aprobado (payment_id={mp_payment_id}) por {tr_amount} {currency}"
            })
            db.commit()
            print(f"[pagos/mp] Pedido #{id_pedido} marcado pagado por webhook.")
        else:
            # ---- Opcional: reflejar estados no-aprobados en pedido_pagos.estado
            estado_map = {
                "pending": "pendiente",
                "in_process": "pendiente",
                "rejected": "rechazado",
                "cancelled": "cancelado",
                "refunded": "reembolsado",
                "charged_back": "contracargo",
            }
            nuevo_estado = estado_map.get(status)
            if nuevo_estado:
                db.execute(text("""
                    UPDATE public.pedido_pagos
                       SET estado=:estado
                     WHERE id_pago=:id_pago
                """), {"estado": nuevo_estado, "id_pago": id_pago})
                db.commit()
                print(f"[pagos/mp] pedido_pagos #{id_pago} estado -> {nuevo_estado}")
            else:
                print(f"[pagos/mp] Estado no aprobado/no mapeado: {status} (sin cambios en cabecera)")
    except Exception as e:
        db.rollback()
        print("[pagos/mp] ERROR actualizando pedido tras webhook:", e)
        return {"ok": False}

    return {"ok": True}

@router.get("/admin/pagos/mp/health")
def mp_health(admin_user: dict = Depends(require_admin)):
    from app.services.mercadopago import whoami
    info = whoami()
    print("[pagos/mp] whoami:", info.get("id"), info.get("email"), info.get("site_id"))
    return {"ok": True, "account_id": info.get("id"), "site": info.get("site_id")}
