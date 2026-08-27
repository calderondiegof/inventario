-- ============================================================
-- MIGRACIÓN: FLUJO "ORDEN DE SALIDA" → "REMISIÓN APROBADA"
-- ============================================================
-- Soporta el ciclo de aprobación con precios:
--   ORDEN_SALIDA → PENDIENTE_PRECIOS → APROBADA | ANULADA
--
-- Cambios:
--   1) remisiones.estado               → TEXT con los estados del flujo oficial.
--   2) remisiones.vr_dolar_dia        → NUMERIC nullable (tipo de cambio del día).
--   3) remisiones.numero              → semántica nueva (OS-1001 → REM-1001 / OS-1001).
--   4) movimientos_inventario.precio_unitario → NUMERIC nullable (precio/kg contabilidad).
--   5) RPC aprobar_remision_con_precios       → transacción que aprueba remisión + precios.
--
-- Ejecutar en el SQL Editor de Supabase. Idempotente (se puede correr
-- varias veces sin romper datos). Replica el estilo de las otras migraciones.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------------
-- 1) remisiones.estado
--    Asegura la columna y agrega una restricción CHECK con los valores
--    del flujo nuevo. Incluye 'ACTIVA' y 'MODIFICADA' (legacy) para NO
--    romper el código actual del servicio, que aún las escribe.
-- ------------------------------------------------------------------
ALTER TABLE remisiones
    ADD COLUMN IF NOT EXISTS estado TEXT DEFAULT 'ORDEN_SALIDA';

ALTER TABLE remisiones
    DROP CONSTRAINT IF EXISTS remisiones_estado_check;

ALTER TABLE remisiones
    ADD CONSTRAINT remisiones_estado_check
    CHECK (estado IN (
        'ORDEN_SALIDA',       -- orden de salida creada
        'PENDIENTE_PRECIOS',  -- lista para que contabilidad cargue precios
        'APROBADA',           -- aprobada con vr_dolar_dia y precios por línea
        'ANULADA',            -- anulada
        'ACTIVA',             -- legacy (servicio actual)
        'MODIFICADA'          -- legacy (servicio actual)
    ));

COMMENT ON COLUMN remisiones.estado IS
    'Flujo: ORDEN_SALIDA → PENDIENTE_PRECIOS → APROBADA. Legacy ACTIVA/MODIFICADA por compatibilidad.';

-- ------------------------------------------------------------------
-- 2) remisiones.vr_dolar_dia  (NUMERIC, NULLABLE)
-- ------------------------------------------------------------------
ALTER TABLE remisiones
    ADD COLUMN IF NOT EXISTS vr_dolar_dia NUMERIC;

COMMENT ON COLUMN remisiones.vr_dolar_dia IS
    'Tipo de cambio del dólar del día; lo fija la RPC aprobar_remision_con_precios al aprobar.';

-- ------------------------------------------------------------------
-- 3) remisiones.numero  (semántica nueva; la columna ya es TEXT)
--    Ahora representa la Orden de Salida (ej. OS-1001) y, al aprobar,
--    se conserva/usa como número final de Remisión (ej. REM-1001 / OS-1001).
-- ------------------------------------------------------------------
ALTER TABLE remisiones
    ALTER COLUMN numero TYPE TEXT;

COMMENT ON COLUMN remisiones.numero IS
    'Número de la Orden de Salida (ej. OS-1001). Al aprobar se mantiene como OS-1001 / REM-1001.';

-- ------------------------------------------------------------------
-- 4) movimientos_inventario.precio_unitario  (NUMERIC, NULLABLE)
--    Ya se inserta desde registrar_venta_multiple; se asegura por si en
--    bases nuevas no existiera.
-- ------------------------------------------------------------------
ALTER TABLE movimientos_inventario
    ADD COLUMN IF NOT EXISTS precio_unitario NUMERIC;

COMMENT ON COLUMN movimientos_inventario.precio_unitario IS
    'Precio por kilogramo. Lo carga contabilidad al aprobar la remisión (RPC aprobar_remision_con_precios).';

-- Índice de apoyo para asociar movimientos a un lote (usado por la RPC y las
-- consultas de remisión existentes). Idempotente.
CREATE INDEX IF NOT EXISTS idx_movimientos_lote_operacion
    ON movimientos_inventario (lote_operacion_id);

-- ------------------------------------------------------------------
-- 5) RPC: aprobar_remision_con_precios
--    Params: p_remision_id INT, p_vr_dolar_dia NUMERIC,
--            p_precio_items JSONB  →  {movimiento_id: precio_unitario, ...}
--    Lógica transaccional (atómica):
--      (a) fija remisiones.vr_dolar_dia = p_vr_dolar_dia y estado = 'APROBADA';
--      (b) actualiza en movimientos_inventario el precio_unitario SOLO de los
--          movimientos del lote de esa remisión (integridad: no toca otros lotes).
--    Devuelve un resumen JSONB con el número de movimientos actualizados.
-- ----------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.aprobar_remision_con_precios(
    p_remision_id INTEGER,
    p_vr_dolar_dia NUMERIC,
    p_precios_items JSONB
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_numero  TEXT;
    v_lote    TEXT;
    v_precio  NUMERIC;
    v_mov_id  INTEGER;
    v_updated INTEGER := 0;
BEGIN
    -- Validación básica de parámetros.
    IF p_remision_id IS NULL THEN
        RAISE EXCEPTION 'El parámetro p_remision_id es obligatorio.';
    END IF;

    IF p_precios_items IS NOT NULL AND jsonb_typeof(p_precios_items) <> 'object' THEN
        RAISE EXCEPTION 'p_precios_items debe ser un objeto JSONB {movimiento_id: precio_unitario}.';
    END IF;

    -- Bloquear la remisión y capturar su lote (FOR UPDATE evita carreras).
    SELECT numero, lote_operacion_id
      INTO v_numero, v_lote
      FROM public.remisiones
     WHERE id = p_remision_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No existe la remisión con id %.', p_remision_id;
    END IF;

    -- (a) Fijar tipo de cambio del día y estado APROBADA.
    UPDATE public.remisiones
       SET vr_dolar_dia = p_vr_dolar_dia,
           estado       = 'APROBADA'
     WHERE id = p_remision_id;

    -- (b) Aplicar precios por movimiento, restringido al lote de la remisión.
    FOR v_mov_id, v_precio IN
        SELECT (key)::INTEGER, (value)::NUMERIC
          FROM jsonb_each_text(COALESCE(p_precios_items, '{}'::jsonb))
    LOOP
        UPDATE public.movimientos_inventario
           SET precio_unitario = v_precio
         WHERE id = v_mov_id
           AND lote_operacion_id = v_lote;

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'Movimiento % no pertenece al lote de la remisión %.',
                v_mov_id, v_numero;
        END IF;

        v_updated := v_updated + 1;
    END LOOP;

    RETURN jsonb_build_object(
        'remision_id',           p_remision_id,
        'numero',                v_numero,
        'estado',                'APROBADA',
        'vr_dolar_dia',          p_vr_dolar_dia,
        'movimientos_precios',   v_updated
    );
END;
$$;

COMMIT;

-- ============================================================
-- NOTAS
-- ============================================================
-- * Se invoca desde el backend con `supabase.rpc('aprobar_remision_con_precios',
--   {p_remision_id: ..., p_vr_dolar_dia: ..., p_precios_items: {...}})`.
-- * El filtro `lote_operacion_id` garantiza que un movimiento de otro lote sea
--   rechazado (integridad) y NO se precie por error.
-- * Si p_precios_items viene vacío, la RPC solo aprueba la remisión sin cargar
--   precios (los deja como estaban).
-- ============================================================