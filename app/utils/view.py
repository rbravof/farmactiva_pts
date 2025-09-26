# app/utils/view.py
from typing import Dict, Any, Optional
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

def render_admin(
    templates: Jinja2Templates,
    request: Request,
    template_name: str,
    context: Optional[Dict[str, Any]] = None,
    user: Optional[dict] = None,
) -> HTMLResponse:
    """
    Renderiza una plantilla Jinja2 agregando 'request' y 'user' automáticamente.
    Úsalo en todas las vistas del panel Admin.
    """
    ctx: Dict[str, Any] = {"request": request, "user": user}
    if context:
        ctx.update(context)
    return templates.TemplateResponse(template_name, ctx)
