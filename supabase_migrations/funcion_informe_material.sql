-- ============================================================
-- FUNCIÓN: obtener_informe_material( material_nombre TEXT,
--                                     bodega_id INT,
--                                     fecha_desde DATE,
--                                     fecha_hasta DATE )
-- RETORNO: RECORD con (saldo_inicial, entradas JSONB,
--                      salidas JSONB, total_entradas,
--                      total_salidas, movimiento_periodo,
--                      saldo_final)
-- USO: SELECT * FROM obtener_informe_material('Revuelto', 1,
--            '2026-09-09'::date, '2026-09-09'::date);
-- OBS: Agrupa entradas por fuentes_origen.nombre y salidas
--      por observaciones (ej: 'Selección', 'Remisiones').
--      Implementación alternativa a la versión Python en
--      services/inventario_service.py::obtener_informe_material.
-- ============================================================
CREATE OR REPLACE FUNCTION public.obtener_informe_material(
    p_material_nombre TEXT,
    p_bodega_id      INT,
    p_fecha_desde    DATE,
    p_fecha_hasta    DATE
)
RETURNS TABLE (
    saldo_inicial    NUMERIC,
    entradas         JSONB,
    salidas          JSONB,
    total_entradas   NUMERIC,
    total_salidas    NUMERIC,
    movimiento_periodo NUMERIC,
    saldo_final      NUMERIC
)
LANGUAGE SQL
STABLE
AS $$
WITH parametros AS (
    SELECT
        (SELECT id FROM materiales WHERE nombre = p_material_nombre) AS material_id,
        p_bodega_id AS bodega_id,
        p_fecha_desde AS fecha_desde,
        p_fecha_hasta AS fecha_hasta
),
base AS (
    SELECT
        m.cantidad_kg,
        m.fecha_operacion,
        m.observaciones,
        COALESCE(fo.nombre, 'Sin fuente') AS fuente_nombre
    FROM movimientos_inventario m
    CROSS JOIN parametros p
    LEFT JOIN fuentes_origen fo ON fo.id = m.fuente_id
    WHERE m.bodega_id = p.bodega_id
      AND m.material_id = p.material_id
),
saldo_inicial AS (
    SELECT COALESCE(SUM(b.cantidad_kg), 0) AS saldo
    FROM base b, parametros p
    WHERE b.fecha_operacion < p.fecha_desde
),
periodo AS (
    SELECT b.*
    FROM base b, parametros p
    WHERE b.fecha_operacion >= p.fecha_desde
      AND b.fecha_operacion < (p.fecha_hasta + INTERVAL '1 day')
),
entradas AS (
    SELECT fuente_nombre AS etiqueta, SUM(cantidad_kg) AS kg
    FROM periodo
    WHERE cantidad_kg > 0
    GROUP BY fuente_nombre
),
salidas AS (
    SELECT COALESCE(observaciones, 'Sin observación') AS etiqueta,
           ABS(SUM(cantidad_kg)) AS kg
    FROM periodo
    WHERE cantidad_kg < 0
    GROUP BY COALESCE(observaciones, 'Sin observación')
),
totales AS (
    SELECT
        COALESCE(SUM(CASE WHEN cantidad_kg > 0 THEN cantidad_kg END), 0) AS total_entradas,
        COALESCE(SUM(CASE WHEN cantidad_kg < 0 THEN ABS(cantidad_kg) END), 0) AS total_salidas
    FROM periodo
)
SELECT
    si.saldo AS saldo_inicial,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object('etiqueta', etiqueta, 'kg', kg) ORDER BY kg DESC), '[]'::jsonb)
     FROM entradas) AS entradas,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object('etiqueta', etiqueta, 'kg', kg) ORDER BY kg DESC), '[]'::jsonb)
     FROM salidas) AS salidas,
    t.total_entradas,
    t.total_salidas,
    t.total_entradas - t.total_salidas AS movimiento_periodo,
    si.saldo + (t.total_entradas - t.total_salidas) AS saldo_final
FROM saldo_inicial si, totales t;
$$;

-- Permisos para el rol anonimo (Supabase Edge)
GRANT EXECUTE ON FUNCTION public.obtener_informe_material(
    TEXT, INT, DATE, DATE
) TO anon;
