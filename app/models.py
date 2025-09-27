# app/models.py
from __future__ import annotations
from typing import List, Optional
from datetime import datetime
from decimal import Decimal  # <-- NUEVO

from sqlalchemy import (
    DateTime, func, JSON,
    Boolean, CheckConstraint, Column, ForeignKey, Integer, Numeric, String, Text,
    TIMESTAMP, UniqueConstraint, text, SmallInteger, Index, BigInteger
)
from sqlalchemy.orm import relationship, Mapped, mapped_column  # <-- sin declarative_base
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import CITEXT

from app.database import Base  # <-- usa la Base central del proyecto


# ---------------------------------------
# Mixin de timestamps
# ---------------------------------------
class TimestampMixin:
    # Usar DateTime + datetime; defaults en DB; onupdate en ORM o trigger
    fecha_creacion: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    fecha_actualizacion: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

class TipoMedicamento(Base):
    """
    Catálogo de tipos de medicamento (ej: Marca, Bioequivalente, Genérico).
    Coincide con la tabla:
      public.tipo_medicamento (
        id_tipo_medicamento SERIAL PK,
        codigo VARCHAR(50) NULL,
        nombre VARCHAR(120) NOT NULL UNIQUE
      )
    """
    __tablename__ = "tipo_medicamento"
    # Nota: si quieres fijar esquema explícito, descomenta:
    # __table_args__ = {"schema": "public"}

    id_tipo_medicamento: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)

    # Relación 1–1 con el margen PTS (tabla: public.pts_margenes)
    margen_pts: Mapped[Optional["PtsMargen"]] = relationship(
        "PtsMargen",
        back_populates="tipo_medicamento",
        uselist=False,
        lazy="joined",
        cascade="all, delete-orphan",
        primaryjoin="TipoMedicamento.id_tipo_medicamento == PtsMargen.id_tipo_medicamento",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"TipoMedicamento(id={self.id_tipo_medicamento}, codigo={self.codigo!r}, nombre={self.nombre!r})"


# (Opcional pero recomendado) Clase PtsMargen para que la relación compile.
# Debe corresponder a la tabla propuesta `public.pts_margenes`:
#   id_tipo_medicamento INTEGER PK/FK -> tipo_medicamento.id_tipo_medicamento
#   margen NUMERIC(8,5) NOT NULL
#   timestamps
class PtsMargen(Base):
    __tablename__ = "pts_margenes"
    # __table_args__ = {"schema": "public"}  # si deseas fijar esquema

    id_tipo_medicamento: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tipo_medicamento.id_tipo_medicamento", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    margen: Mapped["Decimal"] = mapped_column(Numeric(8, 5), nullable=False)
    creado_en: Mapped["datetime"] = mapped_column(DateTime(timezone=False), server_default=text("now()"), nullable=False)
    actualizado_en: Mapped[Optional["datetime"]] = mapped_column(DateTime(timezone=False))

    tipo_medicamento: Mapped["TipoMedicamento"] = relationship(
        "TipoMedicamento",
        back_populates="margen_pts",
        lazy="joined",
        primaryjoin="PtsMargen.id_tipo_medicamento == TipoMedicamento.id_tipo_medicamento",
    )

    def __repr__(self) -> str:
        return f"PtsMargen(tipo={self.id_tipo_medicamento}, margen={self.margen})"

class AppParametro(Base):
    """
    Parámetros simples de aplicación (clave/valor).
    Ej: clave='pts_margen_default', valor='0.08'
    """
    __tablename__ = "app_parametros"

    clave: Mapped[str] = mapped_column(String(80), primary_key=True)
    valor: Mapped[str] = mapped_column(Text, nullable=False)

    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"AppParametro(clave='{self.clave}', valor='{self.valor}')"

class PtsMargenCategoria(Base):
    __tablename__ = "pts_margenes_cat"

    id_categoria: Mapped[int] = mapped_column(
        Integer, ForeignKey("categorias.id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True
    )
    margen: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime, onupdate=func.now())

    categoria: Mapped["Categoria"] = relationship(
        "Categoria",
        back_populates="margen_categoria",
        primaryjoin="PtsMargenCategoria.id_categoria == Categoria.id",
        lazy="joined",
    )

class DireccionEnvio(Base):
    __tablename__ = "direcciones_envio"

    id_direccion   = Column(Integer, primary_key=True, autoincrement=True)
    # Si quieres asociar a un cliente:
    id_cliente     = Column(Integer, ForeignKey("clientes.id_cliente"), nullable=True)

    # Datos de contacto y dirección
    nombre         = Column(String(200), nullable=True)
    telefono       = Column(String(50), nullable=True)

    calle          = Column(String(200), nullable=True)
    numero         = Column(String(50), nullable=True)
    depto          = Column(String(50), nullable=True)

    comuna         = Column(String(120), nullable=True)
    ciudad         = Column(String(120), nullable=True)
    region         = Column(String(120), nullable=True)
    codigo_postal  = Column(String(20), nullable=True)
    pais           = Column(String(80), nullable=True, default="Chile")

    referencia     = Column(Text, nullable=True)

    creado_en      = Column(DateTime, server_default=func.now(), nullable=False)
    actualizado_en = Column(DateTime, server_default=func.now(),
                            onupdate=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<DireccionEnvio id={self.id_direccion} {self.calle or ''} {self.numero or ''}>"


# ===========================
# BODEGAS
# ===========================
class Bodega(Base):
    __tablename__ = "bodegas"

    id_bodega: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)

    calle_numero: Mapped[str | None] = mapped_column(String(180))
    referencia: Mapped[str | None] = mapped_column(String(180))
    id_region: Mapped[int | None] = mapped_column(Integer, ForeignKey("regiones.id_region"))
    id_comuna: Mapped[int | None] = mapped_column(Integer, ForeignKey("comunas.id_comuna"))

    lat: Mapped[float | None] = mapped_column()
    lon: Mapped[float | None] = mapped_column()

    encargado_nombre: Mapped[str | None] = mapped_column(String(120))
    encargado_email: Mapped[str | None] = mapped_column(String(180))
    encargado_telefono: Mapped[str | None] = mapped_column(String(30))

    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    orden: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<Bodega {self.id_bodega} {self.nombre!r}>"
    
# ===========================
# CLIENTES
# ===========================
class Cliente(Base):
    __tablename__ = "clientes"

    id_cliente: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)

    # Únicos (permiten NULL)
    rut: Mapped[Optional[str]] = mapped_column(String(12), unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(180), unique=True)

    telefono: Mapped[Optional[str]] = mapped_column(String(30))
    notas: Mapped[Optional[str]] = mapped_column(Text)

    acepta_marketing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    # Direcciones normalizadas (1:N)
    direcciones: Mapped[list["ClienteDireccion"]] = relationship(
        "ClienteDireccion",
        back_populates="cliente",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Pedidos (1:N)
    pedidos: Mapped[List["Pedido"]] = relationship(
        back_populates="cliente",
        cascade="save-update, merge",
        passive_deletes=True
    )

    __table_args__ = (
        Index("idx_clientes_nombre", text("lower(nombre)")),
        Index("idx_clientes_email", text("lower(email)")),
        Index("idx_clientes_rut", "rut"),
    )

    def __repr__(self) -> str:
        return f"<Cliente {self.id_cliente} {self.nombre!r}>"

class TipoDireccion(Base):
    __tablename__ = "tipos_direccion"
    id_tipo_direccion: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    nombre: Mapped[str] = mapped_column(String(60), nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

class ClienteDireccion(Base):
    __tablename__ = "clientes_direcciones"
    id_direccion: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_cliente: Mapped[int] = mapped_column(ForeignKey("clientes.id_cliente", ondelete="CASCADE"), index=True)

    etiqueta: Mapped[str | None] = mapped_column(String(60))
    calle_numero: Mapped[str] = mapped_column(String(180))
    depto: Mapped[str | None] = mapped_column(String(80))
    referencia: Mapped[str | None] = mapped_column(String(200))
    id_region: Mapped[int] = mapped_column(ForeignKey("regiones.id_region"))
    id_comuna: Mapped[int] = mapped_column(ForeignKey("comunas.id_comuna"))

    # Nuevo (opcional pero recomendado)
    id_tipo_direccion: Mapped[int | None] = mapped_column(ForeignKey("tipos_direccion.id_tipo_direccion"))

    es_principal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    cliente: Mapped["Cliente"] = relationship("Cliente", back_populates="direcciones")
    # (Opcional) relationships a catálogo y ubigeo
    tipo: Mapped["TipoDireccion"] = relationship("TipoDireccion")

# -------------------------------------------------
# Regiones
# -------------------------------------------------
class Region(Base):
    __tablename__ = "regiones"
    __table_args__ = {"schema": "public"}

    id_region: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str]    = mapped_column(CITEXT(), nullable=False, unique=True)
    abreviatura: Mapped[str | None] = mapped_column(String(10))
    orden: Mapped[int]     = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    activo: Mapped[bool]   = mapped_column(Boolean, nullable=False, server_default=text("true"))
    fecha_creacion: Mapped[DateTime] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())

    comunas: Mapped[list["Comuna"]] = relationship(
        "Comuna",
        back_populates="region",
        lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Region id={self.id_region} nombre={self.nombre!r}>"


# -------------------------------------------------
# Comunas
# -------------------------------------------------
class Comuna(Base):
    __tablename__ = "comunas"
    __table_args__ = (
        UniqueConstraint("id_region", "nombre", name="comunas_region_nombre_uk"),
        {"schema": "public"},
    )

    id_comuna: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_region: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("public.regiones.id_region", ondelete="CASCADE"),
        nullable=False,
    )
    nombre: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    orden: Mapped[int]  = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    fecha_creacion: Mapped[DateTime] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())

    region: Mapped["Region"] = relationship(
        "Region",
        back_populates="comunas",
        lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<Comuna id={self.id_comuna} nombre={self.nombre!r} region={self.id_region}>"

# ===========================
# ENVIOS & TARIFAS
# ===========================
# --- Tipos de envío y Tarifas ---
class TipoEnvio(Base):
    __tablename__ = "tipos_envio"
    id_tipo_envio: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo:        Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    nombre:        Mapped[str] = mapped_column(String(80), nullable=False)
    requiere_direccion: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    activo:        Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    orden:         Mapped[int]  = mapped_column(Integer, nullable=False, server_default=text("0"))
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<TipoEnvio {self.codigo}>"

class EnvioTarifa(Base):
    __tablename__ = "envio_tarifas"
    id_tarifa:     Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_tipo_envio: Mapped[int] = mapped_column(ForeignKey("tipos_envio.id_tipo_envio", ondelete="CASCADE"), nullable=False)
    id_region:     Mapped[Optional[int]] = mapped_column(ForeignKey("regiones.id_region"))
    id_comuna:     Mapped[Optional[int]] = mapped_column(ForeignKey("comunas.id_comuna"))
    base_clp:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gratis_desde:  Mapped[Optional[int]] = mapped_column(Integer)
    peso_min_g:    Mapped[Optional[int]] = mapped_column(Integer)
    peso_max_g:    Mapped[Optional[int]] = mapped_column(Integer)
    prioridad:     Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    activo:        Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    tipo_envio:    Mapped["TipoEnvio"] = relationship(backref="tarifas")

# ===========================
# PEDIDOS
# ===========================
class Pedido(Base):
    __tablename__ = "pedidos"

    id_pedido: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Número visible del pedido (se genera en backend o con función SQL)
    numero: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        # server_default=text("public.next_pedido_numero()")  # descomenta si usas la función en DB
    )

    # Cliente (NULL si se borra)
    id_cliente: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("clientes.id_cliente", ondelete="SET NULL")
    )
    cliente: Mapped[Optional["Cliente"]] = relationship(back_populates="pedidos")

    # Canal y estado
    canal: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'manual'"))
    estado_codigo: Mapped[str] = mapped_column(
        String(40), ForeignKey("pedido_estados.codigo", ondelete="RESTRICT"), nullable=False
    )
    estado: Mapped["PedidoEstado"] = relationship()

    # --- Envío (nuevo) ---
    # Tipo de despacho seleccionado (retiro/normal/express, etc.)
    id_tipo_envio: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tipos_envio.id_tipo_envio", ondelete="RESTRICT")
    )
    tipo_envio: Mapped[Optional["TipoEnvio"]] = relationship()

    # Dirección de envío elegida del cliente (puede quedar NULL p.ej. retiro en tienda)
    id_direccion_envio: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clientes_direcciones.id_direccion", ondelete="SET NULL")
    )
    direccion_envio: Mapped[Optional["ClienteDireccion"]] = relationship()

    # Costo de envío calculado al crear/editar el pedido (CLP enteros)
    costo_envio: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Importe total (suma ítems + costo_envio, si así lo manejas)
    total_neto: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), info={"units": "CLP"}
    )

    # Timestamps
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    # Relaciones detalle / historial / notas
    items: Mapped[List["PedidoItem"]] = relationship(
        back_populates="pedido", cascade="all, delete-orphan", passive_deletes=True
    )
    historial: Mapped[List["PedidoHistorial"]] = relationship(
        back_populates="pedido",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PedidoHistorial.creado_en",
    )
    notas: Mapped[List["PedidoNota"]] = relationship(
        back_populates="pedido",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PedidoNota.creado_en",
    )
    # relación con pagos
    pagos: Mapped[List["PedidoPago"]] = relationship(
        "PedidoPago",
        back_populates="pedido",
        cascade="all, delete-orphan",
        passive_deletes=True,
        primaryjoin="Pedido.id_pedido==PedidoPago.id_pedido",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Pedido {self.id_pedido} #{self.numero} estado={self.estado_codigo}>"

# ===========================
# PEDIDO · Ítems
# ===========================
class PedidoItem(Base):
    __tablename__ = "pedido_items"

    id_item: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    id_pedido: Mapped[int] = mapped_column(
        Integer, ForeignKey("pedidos.id_pedido", ondelete="CASCADE"),
        nullable=False, index=True
    )
    pedido: Mapped["Pedido"] = relationship(back_populates="items")

    id_producto: Mapped[int] = mapped_column(
        Integer, ForeignKey("productos.id_producto", ondelete="RESTRICT"),
        nullable=False, index=True
    )

    # Cache del nombre para reportes (la BD lo tiene NOT NULL)
    nombre_producto: Mapped[str] = mapped_column(String(255), nullable=False)

    cantidad: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    precio_unitario: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    # 🔴 AHORA ES COLUMNA REAL (coincide con tu BD: NOT NULL)
    subtotal: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("cantidad > 0", name="ck_pedido_items_cantidad_pos"),
        CheckConstraint("precio_unitario >= 0", name="ck_pedido_items_precio_pos"),
        CheckConstraint("subtotal >= 0", name="ck_pedido_items_subtotal_pos"),
        Index("idx_pedido_items_pedido", "id_pedido"),
    )

    @property
    def subtotal_calc(self) -> int:
        """Cálculo auxiliar por si quieres mostrarlo sin depender de la columna."""
        try:
            return int(self.cantidad or 0) * int(self.precio_unitario or 0)
        except Exception:
            return 0

    def __repr__(self) -> str:
        return f"<PedidoItem {self.id_item} pedido={self.id_pedido} prod={self.id_producto} x{self.cantidad}>"

# ===========================
# PEDIDO · Historial de cambios
# ===========================
class PedidoHistorial(Base):
    __tablename__ = "pedido_historial"

    id_historial: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_pedido: Mapped[int] = mapped_column(Integer, ForeignKey("pedidos.id_pedido", ondelete="CASCADE"), nullable=False)
    pedido: Mapped["Pedido"] = relationship(back_populates="historial")

    estado_origen: Mapped[Optional[str]] = mapped_column(String(40))
    estado_destino: Mapped[str] = mapped_column(String(40), nullable=False)

    # Opcional: quién realizó el cambio (si gestionas usuarios)
    # id_usuario: Mapped[Optional[int]] = mapped_column(Integer)

    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<PedidoHistorial {self.id_historial} {self.estado_origen}->{self.estado_destino}>"

# ===========================
# PEDIDO · Notas
# ===========================
class PedidoNota(Base):
    __tablename__ = "pedido_notas"

    id_nota: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_pedido: Mapped[int] = mapped_column(Integer, ForeignKey("pedidos.id_pedido", ondelete="CASCADE"), nullable=False)
    pedido: Mapped["Pedido"] = relationship(back_populates="notas")

    # Nota asociada al HITO/estado destino (para routing visual); puede ser NULL
    estado_codigo_destino: Mapped[Optional[str]] = mapped_column(String(40))

    texto: Mapped[str] = mapped_column(Text, nullable=False)

    # Audiencia: NEXT_ROLE | INTERNAL_ALL | CUSTOMER
    audiencia: Mapped[str] = mapped_column(String(20), nullable=False, default="NEXT_ROLE", server_default=text("'NEXT_ROLE'"))
    destinatario_rol: Mapped[Optional[str]] = mapped_column(String(40))
    visible_para_cliente: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("audiencia in ('NEXT_ROLE','INTERNAL_ALL','CUSTOMER')", name="ck_pedido_notas_audiencia"),
        Index("idx_pedido_notas_pedido", "id_pedido"),
    )

    def __repr__(self) -> str:
        return f"<PedidoNota {self.id_nota} aud={self.audiencia}>"

class PedidoEstado(Base):
    __tablename__ = "pedido_estados"

    # PK smallint con secuencia
    id_estado: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)

    # NOT NULL + longitudes exactas según DDL
    codigo:           Mapped[str] = mapped_column(String(40),  nullable=False, unique=True)
    nombre:           Mapped[str] = mapped_column(String(80),  nullable=False)
    rol_responsable:  Mapped[str] = mapped_column(String(30),  nullable=False)

    # NOT NULL + defaults
    orden:    Mapped[int]  = mapped_column(SmallInteger, nullable=False, server_default=text("0"),    default=0)
    activo:   Mapped[bool] = mapped_column(Boolean,     nullable=False, server_default=text("true"),  default=True)
    es_final: Mapped[bool] = mapped_column(Boolean,     nullable=False, server_default=text("false"), default=False)

    # Relaciones por id_estado (ojo: PedidoTransicion.origen_id/destino_id deben mapear a columnas 'origen'/'destino' SMALLINT)
    transiciones_salida: Mapped[List["PedidoTransicion"]] = relationship(
        "PedidoTransicion",
        back_populates="origen",
        foreign_keys="PedidoTransicion.origen_id",
        cascade="all, delete-orphan",
    )
    transiciones_entrada: Mapped[List["PedidoTransicion"]] = relationship(
        "PedidoTransicion",
        back_populates="destino",
        foreign_keys="PedidoTransicion.destino_id",
        cascade="all, delete-orphan",
    )

    # Compatibilidad: algunos códigos podían leer/escribir rol_owner
    @property
    def rol_owner(self) -> str:
        return self.rol_responsable

    @rol_owner.setter
    def rol_owner(self, value: str) -> None:
        self.rol_responsable = value

    def __repr__(self) -> str:
        return f"<PedidoEstado id={self.id_estado} codigo={self.codigo}>"

class PedidoTransicion(Base):
    __tablename__ = "pedido_estado_transiciones"

    id_transicion: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    origen_id:  Mapped[int] = mapped_column("origen",  SmallInteger,
                                            ForeignKey("pedido_estados.id_estado", ondelete="CASCADE"),
                                            nullable=False)
    destino_id: Mapped[int] = mapped_column("destino", SmallInteger,
                                            ForeignKey("pedido_estados.id_estado", ondelete="CASCADE"),
                                            nullable=False)

    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)

    origen:  Mapped["PedidoEstado"] = relationship("PedidoEstado", foreign_keys=[origen_id],  back_populates="transiciones_salida")
    destino: Mapped["PedidoEstado"] = relationship("PedidoEstado", foreign_keys=[destino_id], back_populates="transiciones_entrada")

    def __repr__(self) -> str:
        return f"<Transicion {self.origen_id} -> {self.destino_id}>"

# --- PAGO: CABECERA ---
class PedidoPago(Base):
    __tablename__ = "pedido_pagos"

    id_pago: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_pedido: Mapped[int] = mapped_column(
        Integer, ForeignKey("pedidos.id_pedido", ondelete="CASCADE"), nullable=False
    )

    proveedor: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'manual'"))
    link_url:  Mapped[Optional[str]] = mapped_column(Text)

    monto:   Mapped[int]  = mapped_column(Integer, nullable=False)
    moneda:  Mapped[str]  = mapped_column(String(10), nullable=False, server_default=text("'CLP'"))
    estado:  Mapped[str]  = mapped_column(String(20), nullable=False, server_default=text("'pendiente'"))
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("now()"))
    pagado_en: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # campos extra que existen en tu BD
    proveedor_payment_id: Mapped[Optional[str]] = mapped_column(Text)
    estado_detalle:       Mapped[Optional[str]] = mapped_column(String(80))

    eventos: Mapped[List["PedidoPagoEvento"]] = relationship(
        "PedidoPagoEvento",
        back_populates="pago",
        cascade="all, delete-orphan",
        lazy="selectin",
        primaryjoin="PedidoPago.id_pago == PedidoPagoEvento.id_pago",
    )

    def __repr__(self) -> str:
        return f"<PedidoPago id_pago={self.id_pago} id_pedido={self.id_pedido} estado={self.estado!r}>"


# --- PAGO: EVENTOS ---
class PedidoPagoEvento(Base):
    __tablename__ = "pedido_pagos_eventos"

    id_evento: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_pago:   Mapped[int] = mapped_column(
        Integer, ForeignKey("pedido_pagos.id_pago", ondelete="CASCADE"), nullable=False
    )

    tipo:        Mapped[str] = mapped_column(String(40), nullable=False)     # 'payment'
    estado:      Mapped[str] = mapped_column(String(20), nullable=False)     # 'approved', 'pending', ...
    estado_detalle:        Mapped[Optional[str]] = mapped_column(String(80))
    proveedor_payment_id:  Mapped[Optional[str]] = mapped_column(Text)
    payload:               Mapped[Optional[dict]] = mapped_column(JSONB)     # jsonb
    creado_en:             Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("now()"))

    # también existen en la tabla (compatibilidad)
    status:        Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'created'"))
    status_detail: Mapped[Optional[str]] = mapped_column(String(64))
    raw_json:      Mapped[Optional[str]] = mapped_column(Text)

    pago: Mapped["PedidoPago"] = relationship(
        "PedidoPago",
        back_populates="eventos",
        primaryjoin="PedidoPagoEvento.id_pago == PedidoPago.id_pago",
        lazy="joined",
    )

    def __repr__(self) -> str:
        return f"<PedidoPagoEvento id_evento={self.id_evento} id_pago={self.id_pago} tipo={self.tipo!r} estado={self.estado!r}>"

# ---------------------------------------
# Catálogo: Usuario y Administrador
# ---------------------------------------
class Usuario(Base):
    """
    Usuarios del sistema (login). Las contraseñas pueden estar en texto
    plano para desarrollo o en hash bcrypt en producción (tu verify_password
    ya contempla ambos casos).
    """
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usuario: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    rut: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)
    contrasena: Mapped[str] = mapped_column(Text, nullable=False)  # hash o texto según entorno
    nombre: Mapped[str | None] = mapped_column(String(120))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"), default=True)

    fecha_creacion: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    # Relación con Administrador (opcional: para navegar desde usuario → admin)
    admin: Mapped["Administrador"] = relationship(
        "Administrador", back_populates="usuario_ref", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("usuario", name="uq_usuarios_usuario"),
        UniqueConstraint("rut", name="uq_usuarios_rut"),
        Index("idx_usuarios_usuario_lower", text("lower(usuario)")),
    )

    def __repr__(self) -> str:
        return f"<Usuario id={self.id} usuario={self.usuario!r} activo={self.activo}>"

    # Helpers opcionales (si quisieras setear hash aquí):
    # def set_password(self, hashed_or_plain: str) -> None:
    #     self.contrasena = hashed_or_plain  # deja el hashing a un servicio externo si prefieres


class Administrador(Base):
    """
    Lista blanca de administradores para el panel /admin.
    No guarda clave; solo indica si el usuario (de la tabla usuarios) tiene rol admin.
    """
    __tablename__ = "administradores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Referencia al campo 'usuario' de la tabla usuarios (es UNIQUE)
    usuario: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("usuarios.usuario", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"), default=True)
    fecha_creacion: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    # Relación inversa (para navegar admin → usuario)
    usuario_ref: Mapped["Usuario"] = relationship("Usuario", back_populates="admin", lazy="joined")

    def __repr__(self) -> str:
        return f"<Administrador usuario={self.usuario!r} activo={self.activo}>"
    
# ---------------------------------------
# Catálogo: Marcas y Categorías
# ---------------------------------------
class Marca(Base):
    __tablename__ = "marcas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(160), unique=True)
    logo_url: Mapped[Optional[str]] = mapped_column(Text)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"), default=True)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)

    fecha_creacion: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    fecha_actualizacion: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # par simétrico con Producto.marca
    productos: Mapped[List["Producto"]] = relationship(
        "Producto",
        back_populates="marca",
        primaryjoin="Producto.id_marca == Marca.id",
        cascade="save-update, merge",
    )

    __table_args__ = (Index("idx_marca_nombre_lower", text("lower(nombre)")),)

class Categoria(Base, TimestampMixin):
    __tablename__ = "categorias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_padre: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categorias.id", ondelete="SET NULL"), nullable=True
    )

    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    slug:   Mapped[Optional[str]] = mapped_column(String(160), unique=True)
    ruta:   Mapped[Optional[str]] = mapped_column(Text)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"), default=True)
    orden:   Mapped[int]  = mapped_column(Integer, nullable=False, server_default=text("0"),   default=0)

    # jerarquía
    padre: Mapped[Optional["Categoria"]] = relationship(
        "Categoria", remote_side=[id], backref="hijas"
    )

    # par con Subcategoria.categoria
    subcategorias: Mapped[List["Subcategoria"]] = relationship(
        "Subcategoria",
        back_populates="categoria",
        primaryjoin="Subcategoria.id_categoria == Categoria.id",
        cascade="all, delete-orphan",
    )

    # par con Producto.categoria (si usas FK directa en productos)
    productos: Mapped[List["Producto"]] = relationship(
        "Producto",
        back_populates="categoria",
        primaryjoin="Producto.categoria_id == Categoria.id",
        viewonly=True,  # evita escrituras accidentales desde aquí
        lazy="selectin",
    )

    # 🔧 NUEVO: par con ProductoCategoria.categoria (para N:N / pivote)
    productos_rel: Mapped[List["ProductoCategoria"]] = relationship(
        "ProductoCategoria",
        back_populates="categoria",
        primaryjoin="ProductoCategoria.id_categoria == Categoria.id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # 1:1 margen de categoría
    margen_categoria: Mapped[Optional["PtsMargenCategoria"]] = relationship(
        "PtsMargenCategoria",
        back_populates="categoria",
        uselist=False,
        primaryjoin="PtsMargenCategoria.id_categoria == Categoria.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (Index("idx_categoria_nombre_lower", text("lower(nombre)")),)


class Subcategoria(Base):
    __tablename__ = "subcategorias"

    id_subcategoria: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_categoria:    Mapped[int] = mapped_column(Integer, ForeignKey("categorias.id", ondelete="CASCADE"), nullable=False)

    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    slug:   Mapped[str] = mapped_column(String(140), nullable=False, index=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"), default=True)
    fecha_creacion: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=func.now())

    # par con Categoria.subcategorias
    categoria: Mapped["Categoria"] = relationship(
        "Categoria",
        back_populates="subcategorias",
        primaryjoin="Subcategoria.id_categoria == Categoria.id",
    )

    # par con Producto.subcategoria (si usas FK directa en productos)
    productos: Mapped[List["Producto"]] = relationship(
        "Producto",
        back_populates="subcategoria",
        primaryjoin="Producto.subcategoria_id == Subcategoria.id_subcategoria",
        viewonly=True,
    )

    __table_args__ = (
        Index("subcategorias_cat_lower_nombre_uk", "id_categoria", text("lower(nombre)"), unique=True),
        Index("subcategorias_cat_slug_uk", "id_categoria", "slug", unique=True),
        Index("subcategorias_id_categoria_idx", "id_categoria"),
    )

class ProductoCategoria(Base):
    """
    Tabla pivote N:N entre productos y categorías.
    Coincide con la definición de BD (PK compuesta: id_producto, id_categoria).
    """
    __tablename__ = "productos_categorias"

    id_producto: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("productos.id_producto", ondelete="CASCADE"),
        primary_key=True,
    )
    id_categoria: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("categorias.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Nota: no declaramos relaciones aquí porque el modelo actual usa FK directa en Producto (categoria_id).
    # Si más adelante necesitas navegar la N:N, podemos agregar relationships viewonly.
    def __repr__(self) -> str:  # pragma: no cover
        return f"ProductoCategoria(prod={self.id_producto}, cat={self.id_categoria})"

# ---------------------------------------
# Productos, Opciones, Variantes
# ---------------------------------------
class Producto(Base):
    __tablename__ = "productos"

    id_producto: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug:        Mapped[Optional[str]] = mapped_column(String(200))

    # texto principal
    titulo:     Mapped[str] = mapped_column(Text, nullable=False)
    subtitulo:  Mapped[Optional[str]] = mapped_column(Text)
    descripcion_html: Mapped[Optional[str]] = mapped_column(Text)

    # SEO
    seo_titulo:      Mapped[Optional[str]] = mapped_column(String(120))
    seo_descripcion: Mapped[Optional[str]] = mapped_column(String(160))

    # flags / medidas
    visible_web:      Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    requiere_receta:  Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    es_medicamento:   Mapped[Optional[bool]] = mapped_column(Boolean, server_default=text("false"))
    codigo_atc:       Mapped[Optional[str]] = mapped_column(String(64))

    peso_gramos: Mapped[Optional[int]] = mapped_column(Integer)
    ancho_mm:    Mapped[Optional[int]] = mapped_column(Integer)
    alto_mm:     Mapped[Optional[int]] = mapped_column(Integer)
    largo_mm:    Mapped[Optional[int]] = mapped_column(Integer)

    # fechas
    fecha_creacion:      Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=func.now())
    fecha_actualizacion: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # FKs organizacionales
    categoria_id:       Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("categorias.id", ondelete="SET NULL"))
    subcategoria_id:    Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("subcategorias.id_subcategoria", ondelete="SET NULL"))
    id_marca:           Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("marcas.id", ondelete="SET NULL"))
    id_tipo_medicamento: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tipo_medicamento.id_tipo_medicamento"))

    # Imagen y costos
    imagen_principal_url: Mapped[Optional[str]] = mapped_column(Text)
    costo_neto:      Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    costo_promedio:  Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    costo_ultimo:    Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))

    # Relaciones 1:N / N:1
    marca: Mapped[Optional["Marca"]] = relationship(
        "Marca",
        back_populates="productos",
        primaryjoin="Producto.id_marca == Marca.id",
        lazy="joined",
    )

    categoria: Mapped[Optional["Categoria"]] = relationship(
        "Categoria",
        back_populates="productos",
        primaryjoin="Producto.categoria_id == Categoria.id",
        lazy="joined",
    )

    subcategoria: Mapped[Optional["Subcategoria"]] = relationship(
        "Subcategoria",
        back_populates="productos",
        primaryjoin="Producto.subcategoria_id == Subcategoria.id_subcategoria",
        lazy="joined",
    )

    # Relación con códigos de barra
    codigos_barras: Mapped[List["ProductoCodigoBarra"]] = relationship(
        "ProductoCodigoBarra",
        back_populates="producto",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # 🔧 Relaciones que faltaban (coinciden con back_populates definidos en otras clases)
    categorias_rel: Mapped[List["ProductoCategoria"]] = relationship(
        "ProductoCategoria",
        back_populates="producto",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    opciones: Mapped[List["OpcionProducto"]] = relationship(
        "OpcionProducto",
        back_populates="producto",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    variantes: Mapped[List["VarianteProducto"]] = relationship(
        "VarianteProducto",
        back_populates="producto",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    imagenes: Mapped[List["ImagenProducto"]] = relationship(
        "ImagenProducto",
        back_populates="producto",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    etiquetas: Mapped[List["EtiquetaProducto"]] = relationship(
        "EtiquetaProducto",
        back_populates="producto",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Alias opcional para “nombre”
    @hybrid_property
    def nombre(self) -> str:
        return self.titulo

    @nombre.setter
    def nombre(self, value: str) -> None:
        self.titulo = value


class ProductoCodigoBarra(Base):
    __tablename__ = "codigos_barras"
    __table_args__ = (
        UniqueConstraint("codigo_barra", name="codigos_barras_codigo_barra_uk"),
        Index(
            "codigos_barras_unico_principal",
            "id_producto",
            unique=True,
            postgresql_where=text("es_principal = true"),
        ),
    )

    id_codigo: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_producto: Mapped[int] = mapped_column(
        Integer, ForeignKey("productos.id_producto", ondelete="CASCADE"), nullable=False
    )
    codigo_barra: Mapped[str] = mapped_column(String(50), nullable=False)
    es_principal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    fecha_creacion: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=func.now())

    # relación inversa
    producto: Mapped["Producto"] = relationship("Producto", back_populates="codigos_barras", lazy="joined")


class OpcionProducto(Base):
    __tablename__ = "opciones_producto"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_producto: Mapped[int] = mapped_column(
        Integer, ForeignKey("productos.id_producto", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(60), nullable=False)
    posicion: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    producto: Mapped["Producto"] = relationship("Producto", back_populates="opciones")

    def __repr__(self) -> str:
        return f"<OpcionProducto id={self.id} nombre={self.nombre!r}>"


class VarianteProducto(Base, TimestampMixin):
    __tablename__ = "variantes_producto"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_producto: Mapped[int] = mapped_column(
        Integer, ForeignKey("productos.id_producto", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    codigo_barras: Mapped[Optional[str]] = mapped_column(String(64))

    opcion1: Mapped[Optional[str]] = mapped_column(String(120))
    opcion2: Mapped[Optional[str]] = mapped_column(String(120))
    opcion3: Mapped[Optional[str]] = mapped_column(String(120))

    estado: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'activo'")
    )  # activo|borrador|archivado

    peso_gramos: Mapped[Optional[int]] = mapped_column(Integer)
    ancho_mm: Mapped[Optional[int]] = mapped_column(Integer)
    alto_mm: Mapped[Optional[int]] = mapped_column(Integer)
    largo_mm: Mapped[Optional[int]] = mapped_column(Integer)

    requiere_receta: Mapped[Optional[bool]] = mapped_column(Boolean)

    producto: Mapped["Producto"] = relationship("Producto", back_populates="variantes")
    precios: Mapped[List["PrecioVariante"]] = relationship(
        "PrecioVariante", back_populates="variante", cascade="all, delete-orphan"
    )
    inventario: Mapped[List["InventarioVariante"]] = relationship(
        "InventarioVariante", back_populates="variante", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "estado IN ('activo','borrador','archivado')",
            name="ck_variantes_estado",
        ),
    )

    def __repr__(self) -> str:
        return f"<VarianteProducto id={self.id} sku={self.sku!r} estado={self.estado}>"


# ---------------------------------------
# Media y etiquetas
# ---------------------------------------
class ImagenProducto(Base):
    __tablename__ = "imagenes_producto"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_producto: Mapped[int] = mapped_column(
        Integer, ForeignKey("productos.id_producto", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    texto_alternativo: Mapped[Optional[str]] = mapped_column(String(200))
    posicion: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    es_principal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )

    producto: Mapped["Producto"] = relationship("Producto", back_populates="imagenes")

    def __repr__(self) -> str:
        return f"<ImagenProducto id={self.id} principal={self.es_principal}>"


class EtiquetaProducto(Base):
    __tablename__ = "etiquetas_producto"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_producto: Mapped[int] = mapped_column(
        Integer, ForeignKey("productos.id_producto", ondelete="CASCADE"), nullable=False, index=True
    )
    etiqueta: Mapped[str] = mapped_column(String(60), nullable=False)

    producto: Mapped["Producto"] = relationship("Producto", back_populates="etiquetas")

    __table_args__ = (
        Index("idx_etiqueta_valor_lower", text("lower(etiqueta)")),
    )

    def __repr__(self) -> str:
        return f"<EtiquetaProducto id={self.id} etiqueta={self.etiqueta!r}>"


# ---------------------------------------
# Canales, Precios, Sucursales, Inventario
# ---------------------------------------
class CanalVenta(Base):
    __tablename__ = "canales_venta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)  # ej: 'web'
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)

    precios: Mapped[List["PrecioVariante"]] = relationship(
        "PrecioVariante", back_populates="canal", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<CanalVenta id={self.id} codigo={self.codigo!r}>"


class PrecioVariante(Base):
    __tablename__ = "precios_variante"

    id_variante: Mapped[int] = mapped_column(
        Integer, ForeignKey("variantes_producto.id", ondelete="CASCADE"), primary_key=True
    )
    id_canal: Mapped[int] = mapped_column(
        Integer, ForeignKey("canales_venta.id", ondelete="CASCADE"), primary_key=True
    )

    precio: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    precio_comparativo: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    fecha_inicio: Mapped[Optional[str]] = mapped_column(TIMESTAMP)
    fecha_fin: Mapped[Optional[str]] = mapped_column(TIMESTAMP)

    variante: Mapped["VarianteProducto"] = relationship("VarianteProducto", back_populates="precios")
    canal: Mapped["CanalVenta"] = relationship("CanalVenta", back_populates="precios")

    def __repr__(self) -> str:
        return f"<PrecioVariante var={self.id_variante} canal={self.id_canal} precio={self.precio}>"


class Sucursal(Base):
    __tablename__ = "sucursales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)  # ej: 'tienda_web'
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)

    inventarios: Mapped[List["InventarioVariante"]] = relationship(
        "InventarioVariante", back_populates="sucursal", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Sucursal id={self.id} codigo={self.codigo!r}>"


class InventarioVariante(Base):
    __tablename__ = "inventario_variante"

    id_variante: Mapped[int] = mapped_column(
        Integer, ForeignKey("variantes_producto.id", ondelete="CASCADE"), primary_key=True
    )
    id_sucursal: Mapped[int] = mapped_column(
        Integer, ForeignKey("sucursales.id", ondelete="CASCADE"), primary_key=True
    )

    cantidad: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    reservado: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    permite_preventa: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )

    variante: Mapped["VarianteProducto"] = relationship("VarianteProducto", back_populates="inventario")
    sucursal: Mapped["Sucursal"] = relationship("Sucursal", back_populates="inventarios")

    @hybrid_property
    def disponible(self) -> int:
        return int((self.cantidad or 0) - (self.reservado or 0))

    def __repr__(self) -> str:
        return f"<InventarioVariante var={self.id_variante} suc={self.id_sucursal} cant={self.cantidad}>"


# ---------------------------------------
# Utilidad: crear tablas (opcional)
# ---------------------------------------
if __name__ == "__main__":
    from app.database import engine  # en lugar de database_pts
    Base.metadata.create_all(bind=engine)
    print("✅ Tablas creadas/actualizadas")
