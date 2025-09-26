-- ========= app_parametros =========
ALTER TABLE public.app_parametros
    ADD COLUMN IF NOT EXISTS creado_en    timestamp without time zone DEFAULT now();

-- (opcional) si también quieres trackear cambios:
ALTER TABLE public.app_parametros
    ADD COLUMN IF NOT EXISTS actualizado_en timestamp without time zone;

-- ========= categorias =========
ALTER TABLE public.categorias
    ADD COLUMN IF NOT EXISTS fecha_creacion     timestamp without time zone DEFAULT now(),
    ADD COLUMN IF NOT EXISTS fecha_actualizacion timestamp without time zone;

-- trigger para actualizar fecha_actualizacion
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'tr_categorias_touch_updated_at'
  ) THEN
    CREATE OR REPLACE FUNCTION public.fn_touch_categorias_updated_at()
    RETURNS trigger AS $f$
    BEGIN
      NEW.fecha_actualizacion := now();
      RETURN NEW;
    END
    $f$ LANGUAGE plpgsql;

    CREATE TRIGGER tr_categorias_touch_updated_at
      BEFORE UPDATE ON public.categorias
      FOR EACH ROW EXECUTE FUNCTION public.fn_touch_categorias_updated_at();
  END IF;
END$$;

-- ========= direcciones_envio =========
ALTER TABLE public.direcciones_envio
    ADD COLUMN IF NOT EXISTS actualizado_en timestamp without time zone,
    ADD COLUMN IF NOT EXISTS codigo_postal  varchar(20),
    ADD COLUMN IF NOT EXISTS pais           varchar(60);

-- trigger para actualizado_en
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'tr_direnvio_touch_updated_at'
  ) THEN
    CREATE OR REPLACE FUNCTION public.fn_touch_direnvio_updated_at()
    RETURNS trigger AS $f$
    BEGIN
      NEW.actualizado_en := now();
      RETURN NEW;
    END
    $f$ LANGUAGE plpgsql;

    CREATE TRIGGER tr_direnvio_touch_updated_at
      BEFORE UPDATE ON public.direcciones_envio
      FOR EACH ROW EXECUTE FUNCTION public.fn_touch_direnvio_updated_at();
  END IF;
END$$;

-- ========= pedido_notas =========
ALTER TABLE public.pedido_notas
    ADD COLUMN IF NOT EXISTS estado_codigo_destino  varchar(40),
    ADD COLUMN IF NOT EXISTS visible_para_cliente   boolean DEFAULT false;
