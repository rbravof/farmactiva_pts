# app/main.py
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Routers existentes
from app.routers import security, admin_productos, admin_catalogo
from app.routers import auth
from app.routers import catalogo
from app.routers import beneficios
from app.routers import pedidos
from app.routers import admin_security  # panel admin (/admin)
from app.routers import admin_pedidos
from app.routers import admin_clientes
from app.routers import admin_envios
from app.routers import admin_bodegas
from app.routers import admin_precios
from app.routers import admin_menu
from app.routers import admin_pagos
from app.routers import public_pagos
from app.routers import db_tools

app = FastAPI(
    title="Farmactiva · Por tu Salud",
    description="Sistema de beneficio farmacéutico a precio de costo",
    version="1.0.0",
)

# CORS (MVP)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Archivos estáticos y templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ===========================
# Registro de routers
# ===========================
app.include_router(security.router)                    # /login, /logout, get_current_user
app.include_router(auth.router,       prefix="/api/auth",      tags=["Autenticación"])
app.include_router(beneficios.router, prefix="/api/beneficio", tags=["Beneficio"])
app.include_router(catalogo.router)                   # el router ya define prefix="/api/tienda"
app.include_router(pedidos.router,    prefix="/api/pedidos",   tags=["Pedidos"])
app.include_router(admin_security.router)             # /admin, /admin/login, /admin/logout
app.include_router(admin_productos.router)
app.include_router(admin_catalogo.router)
app.include_router(admin_pedidos.router)
app.include_router(admin_clientes.router)
app.include_router(admin_bodegas.router)
app.include_router(admin_envios.router)
app.include_router(admin_envios.api)
app.include_router(admin_precios.router)
app.include_router(admin_menu.router)
app.include_router(admin_pagos.router)
app.include_router(public_pagos.router)
app.include_router(db_tools.router)

# ===========================
# Rutas públicas básicas
# ===========================
@app.get("/", tags=["Landing"], response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

@app.get("/tipo_contrato.html", response_class=HTMLResponse, tags=["Landing"])
def tipo_contrato(request: Request):
    return templates.TemplateResponse("tipo_contrato.html", {"request": request})

# --- Beneficios (página pública) ---
@app.get("/beneficios", response_class=HTMLResponse)
async def beneficios_page(request: Request):
    return templates.TemplateResponse("beneficios.html", {"request": request})

# ===========================
# Tienda / Detalle / Carrito
# ===========================
@app.get("/tienda", response_class=HTMLResponse)
async def tienda(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/producto.html", response_class=HTMLResponse, tags=["Tienda"])
def producto_detalle(request: Request, slug: str | None = None):
    # El template puede resolver el producto por slug vía JS/endpoint público
    return templates.TemplateResponse("producto.html", {"request": request, "slug": slug})

@app.get("/carrito", response_class=HTMLResponse, tags=["Tienda"])
def carrito_view(request: Request):
    return templates.TemplateResponse("carrito.html", {"request": request})

@app.get("/resultados.html", response_class=HTMLResponse, tags=["Tienda"])
def resultados(request: Request, q: str = "", page: int = 1, page_size: int = 12, sort: str = "az"):
    ctx = {"request": request, "q": q, "page": page, "page_size": page_size, "sort": sort}
    return templates.TemplateResponse("resultados.html", ctx)

# ===========================
# API mínima para frontend
# ===========================
@app.get("/api/suscripcion/estado", tags=["Tienda"])
def suscripcion_estado():
    return {"logged": False, "activa": False, "rut": None, "nombre": None}

@app.get("/healthz")
def healthz():
    return {"ok": True}
