# app/services/mercadopago.py
import os
import json
import requests
from typing import Optional, Union, Dict, Any

MP_API_BASE = "https://api.mercadopago.com"


def _bearer() -> Dict[str, str]:
    """Arma el header Authorization Bearer desde MP_ACCESS_TOKEN."""
    token = os.getenv("MP_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Falta MP_ACCESS_TOKEN en .env")
    return {"Authorization": f"Bearer {token}"}


def whoami() -> Dict[str, Any]:
    """Consulta datos de la cuenta asociada al access token."""
    url = f"{MP_API_BASE}/users/me"
    headers = _bearer()
    print(f"[pagos/mp] GET {url}")
    r = requests.get(url, headers=headers, timeout=15)
    print(f"[pagos/mp] RESP {r.status_code}: {r.text[:400]}")
    r.raise_for_status()
    return r.json()


def create_mp_preference(
    *,
    id_pago: int,
    id_pedido: int,
    numero_fmt: str,   # ej "#1010"
    monto: int,        # CLP entero
    moneda: str = "CLP",
    email_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Crea una preferencia de Checkout Pro y devuelve dict con:
    { preference_id, init_point, sandbox_init_point }
    """
    public_base = (os.getenv("PUBLIC_BASE_URL", "") or "").rstrip("/")
    if not public_base:
        raise RuntimeError("PUBLIC_BASE_URL no está seteado; no se puede crear preferencia MP en prod.")

    if public_base.startswith("http://") and "ngrok" not in public_base:
        print("[pagos/mp] WARN: se recomienda HTTPS para MP; puede rechazar callbacks.")

    # Webhook público (necesario para que MP notifique)
    webhook_url = f"{public_base}/integrations/mercadopago/webhook" if public_base else None

    back_success = f"{public_base}/pago/resultado?pedido={id_pedido}&estado=success" if public_base else None
    back_failure = f"{public_base}/pago/resultado?pedido={id_pedido}&estado=failure" if public_base else None
    back_pending = f"{public_base}/pago/resultado?pedido={id_pedido}&estado=pending" if public_base else None

    payload: Dict[str, Any] = {
        "items": [{
            "title": f"Pedido {numero_fmt}",
            "quantity": 1,
            "currency_id": moneda,
            "unit_price": float(monto),
        }],
        "external_reference": str(id_pago),  # nos permitirá casar el webhook con pedido_pagos
        "auto_return": "approved",
        "binary_mode": True,  # fuerza approved/rejected (evita pending); override con env si quieres
    }

    # Permitir override opcional via env
    # MP_BINARY_MODE=0 → desactiva; cualquier otro valor/ausencia → True
    if os.getenv("MP_BINARY_MODE", "").strip() == "0":
        payload.pop("binary_mode", None)

    if email_to:
        payload["payer"] = {"email": email_to}

    if back_success and back_failure and back_pending:
        payload["back_urls"] = {
            "success": back_success,
            "failure": back_failure,
            "pending": back_pending,
        }
    if webhook_url:
        payload["notification_url"] = webhook_url

    url = f"{MP_API_BASE}/checkout/preferences"
    headers = {"Content-Type": "application/json", **_bearer()}
    print(f"[pagos/mp] POST {url} payload={json.dumps(payload, ensure_ascii=False)}")
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    print(f"[pagos/mp] RESP {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()

    data = resp.json()
    return {
        "preference_id": data.get("id"),
        "init_point": data.get("init_point"),                 # link de pago
        "sandbox_init_point": data.get("sandbox_init_point"), # si usas sandbox
    }


def get_mp_payment(payment_id: Union[str, int]) -> Dict[str, Any]:
    """Consulta el pago en MP (v1/payments/{id})."""
    url = f"{MP_API_BASE}/v1/payments/{payment_id}"
    headers = _bearer()
    print(f"[pagos/mp] GET {url}")
    r = requests.get(url, headers=headers, timeout=20)
    print(f"[pagos/mp] RESP {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return r.json()
