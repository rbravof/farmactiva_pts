# app/routers/admin_api.py
from fastapi import APIRouter, Depends, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.routers.admin_security import require_staff
from app.services.mercadopago import create_mp_preference  # <— usa tu helper

api = APIRouter(prefix="/admin/api", tags=["Admin API"])

@api.post("/pagos/mp/preferencias")
def api_mp_crear_preferencia(
    titulo: str = Form("Pedido Farmactiva"),
    descripcion: str = Form(""),
    total_clp: int = Form(...),
    email_cliente: str = Form(""),
    external_reference: str = Form(""),
    success_url: str = Form("/admin/pedidos"),
    failure_url: str = Form("/admin/pedidos"),
    pending_url: str = Form("/admin/pedidos"),
    admin_user: dict = Depends(require_staff),
):
    try:
        pref = create_mp_preference(
            id_pago=None,             # si no tienes aún un id_pago, puedes pasar None
            id_pedido=None,           # idem
            numero_fmt=titulo or "Pedido Farmactiva",
            monto=int(total_clp or 0),
            moneda="CLP",
            email_to=(email_cliente or None),
            back_urls={
                "success": success_url,
                "failure": failure_url,
                "pending": pending_url,
            },
            external_reference=(external_reference or None),
        )
        return {
            "ok": True,
            "id": pref.get("id"),
            "init_point": pref.get("init_point"),
            "sandbox_init_point": pref.get("sandbox_init_point"),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
