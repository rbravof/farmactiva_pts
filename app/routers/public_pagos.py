# app/routers/public_pagos.py
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db

# ⚠️ Importar templates desde aquí evita el ciclo con app.main
templates = Jinja2Templates(directory="app/templates")

router = APIRouter()


def _status_from_params(estado: str | None,
                        status: str | None,
                        collection_status: str | None) -> str:
    """
    Normaliza el estado devuelto por MP a: approved | pending | rejected
    """
    s = (status or collection_status or "").lower().strip()
    if not s and estado:
        s = estado.lower().strip()  # de nuestros back_urls (?estado=success|failure|pending)
        if s == "success":
            s = "approved"
        elif s == "failure":
            s = "rejected"
        elif s == "pending":
            s = "pending"
    return s if s in {"approved", "pending", "rejected"} else "pending"


@router.get("/pago/resultado")
def pago_resultado(
    request: Request,
    pedido: int = Query(..., description="id_pedido"),
    estado: str | None = Query(None),
    status: str | None = Query(None),
    collection_status: str | None = Query(None),
    preference_id: str | None = Query(None),
    payment_id: str | None = Query(None),
    external_reference: str | None = Query(None),
    merchant_order_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    print(f"[PAGO/RESULT] pedido={pedido} estado={estado} status={status} "
          f"collection_status={collection_status} preference_id={preference_id} "
          f"payment_id={payment_id} external_ref={external_reference} order_id={merchant_order_id}")

    # 1) Cabecera del pedido
    header = db.execute(text("""
        SELECT id_pedido, numero, total_neto, pago_estado, pago_proveedor, pago_monto, pago_moneda
        FROM public.pedidos
        WHERE id_pedido = :id
        LIMIT 1
    """), {"id": pedido}).mappings().first()
    if not header:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    # 2) Último intento de pago
    row_pago = db.execute(text("""
        SELECT id_pago, estado, proveedor, link_url
        FROM public.pedido_pagos
        WHERE id_pedido = :id
        ORDER BY id_pago DESC
        LIMIT 1
    """), {"id": pedido}).mappings().first()
    id_pago = row_pago["id_pago"] if row_pago else None

    # 3) Normalizar estado
    norm_status = _status_from_params(estado, status, collection_status)

    # 4) Registrar evento de retorno (si existe tabla e id_pago)
    try:
        if id_pago is not None:
            db.execute(text("""
                INSERT INTO public.pedido_pagos_eventos (id_pago, status, status_detail, raw_json)
                VALUES (:id_pago, :status, :detail, :raw)
            """), {
                "id_pago": id_pago,
                "status": norm_status,
                "detail": (status or collection_status or estado or None),
                "raw": str(dict(request.query_params))
            })
            db.commit()
            print(f"[PAGO/RESULT] evento registrado id_pago={id_pago} status={norm_status}")
    except Exception as e:
        db.rollback()
        print("[PAGO/RESULT] WARN no se pudo registrar evento retorno:", e)

    # 5) (opcional/seguro) verificación directa con MP si viene payment_id aprobado
    if norm_status == "approved" and payment_id and id_pago is not None:
        try:
            from app.services.mercadopago import get_mp_payment
            p = get_mp_payment(payment_id)
            p_status = (p.get("status") or "").lower()
            p_currency = p.get("currency_id") or "CLP"
            p_amount = int(round(float(p.get("transaction_amount") or 0)))
            if p_status == "approved":
                db.execute(text("""
                    UPDATE public.pedido_pagos
                       SET estado='pagado', pagado_en=now(), monto=:monto, moneda=:moneda
                     WHERE id_pago = :id_pago
                """), {"id_pago": id_pago, "monto": p_amount, "moneda": p_currency})
                db.execute(text("""
                    UPDATE public.pedidos
                       SET pago_estado='pagado',
                           pago_proveedor='MercadoPago',
                           pago_monto=:monto,
                           pago_moneda=:moneda
                     WHERE id_pedido=:id_pedido
                       AND COALESCE(pago_estado,'') <> 'pagado'
                """), {"id_pedido": pedido, "monto": p_amount, "moneda": p_currency})
                db.commit()
                print(f"[PAGO/RESULT] marcado pagado por verificación directa payment_id={payment_id}")
        except Exception as e:
            db.rollback()
            print("[PAGO/RESULT] WARN verificación directa MP falló:", e)

    # 6) Render
    return templates.TemplateResponse("pago_resultado.html", {
        "request": request,
        "pedido": header,
        "norm_status": norm_status,
        "payment_id": payment_id,
        "preference_id": preference_id,
        "id_pago": id_pago,
    })
