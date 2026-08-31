-- ============================================================
-- MIGRACIÓN: MÓDULO DE TRANSFORMACIÓN DE MATERIALES (4 ESTADOS)
-- ============================================================
-- Reestructura el catálogo en 4 ESTADOS:
--   BRUTO (A)        -> Revuelto
--   SEMILIMPIO (B)   -> Corte/arreglo/cable/bobinas, etc.
--   LIMPIO (C)       -> Material ya clasificado
--   MERMA (D)        -> Tierra, Basura (AHORA CON STOCK VENDIBLE)
--
-- Requisito: TODOS los materiales (incl. MERMA) deben poder venderse, por lo
-- que Basura/Tierra se marcan como es_comercializable = true y acumulan stock
-- vía  registrar_transformacion_material  (nuevo flujo de guiones en
-- services/inventario_service.py).
--
-- Ejecutar en el SQL Editor de Supabase. Es idempotente (se puede correr
-- varias veces sin romper datos).
-- ============================================================

BEGIN;

-- ------------------------------------------------------------------
-- 1) Aceptar el estado 'MERMA' si el tipo_material es un ENUM/dominio
--    delimitado. Defensivo: ignora el error si el tipo no existe.
-- ------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        ALTER TYPE tipo_material_tipo ADD VALUE IF NOT EXISTS 'MERMA';
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'No se pudo añadir MERMA al tipo tipo_material_tipo: %', SQLERRM;
    END;
    -- Relajar cualquier CHECK constraint sobre tipo_material que prohíba MERMA.
    BEGIN
        EXECUTE (
            SELECT 'ALTER TABLE materiales DROP CONSTRAINT ' || quote_ident(conname)
            FROM pg_constraint
            WHERE conrelid = 'materiales'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%tipo_material%'
              AND pg_get_constraintdef(oid) NOT ILIKE '%MERMA%'
            LIMIT 1
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'No había CHECK de tipo_material que relajar: %', SQLERRM;
    END;
END $$;

-- ------------------------------------------------------------------
-- 2) Reclasificar Basura / Tierra como MERMA comercializable
-- ------------------------------------------------------------------
UPDATE materiales
   SET tipo_material = 'MERMA',
       es_comercializable = true
 WHERE lower(trim(nombre)) IN ('basura', 'tierra');

-- ------------------------------------------------------------------
-- 3) Sembrar / actualizar el catálogo completo por estado
--    (ON CONFLICT asume una restricción única sobre `nombre`).
-- ------------------------------------------------------------------
INSERT INTO materiales (nombre, tipo_material, es_comercializable) VALUES
    -- BRUTO
    ('Revuelto',           'BRUTO',       true),

    -- SEMILIMPIO
    ('Arreglo Cobre y Bronce', 'SEMILIMPIO', true),
    ('Arreglo Aluminio',   'SEMILIMPIO', true),
    ('Arreglo Carter',     'SEMILIMPIO', true),
    ('Cable',              'SEMILIMPIO', true),
    ('Arreglo Antimonio',  'SEMILIMPIO', true),
    ('Bobinas',            'SEMILIMPIO', true),
    ('Plastico',           'SEMILIMPIO', true),
    ('Caucho',             'SEMILIMPIO', true),
    ('Cable Quemado',      'SEMILIMPIO', true),
    ('Arreglo Dificil',    'SEMILIMPIO', true),

    -- LIMPIO
    ('Bronce',             'LIMPIO', true),
    ('Cobre',              'LIMPIO', true),
    ('Carter',             'LIMPIO', true),
    ('Lamina',             'LIMPIO', true),
    ('Antimonio',          'LIMPIO', true),
    ('Plomo',              'LIMPIO', true),
    ('Chatarra',           'LIMPIO', true),
    ('Resistencia',        'LIMPIO', true),
    ('Radiador Cobre',     'LIMPIO', true),
    ('Radiador Bronce',    'LIMPIO', true),
    ('Radiador',           'LIMPIO', true),
    ('Manguera',           'LIMPIO', true),
    ('Tarjeta',            'LIMPIO', true),
    ('Acero',              'LIMPIO', true),
    ('Olla',               'LIMPIO', true),
    ('Perfil',             'LIMPIO', true),
    ('Zinc',               'LIMPIO', true),
    ('Clausen',            'LIMPIO', true),
    ('Baterias',           'LIMPIO', true),

    -- MERMA
    ('Basura',             'MERMA', true),
    ('Tierra',             'MERMA', true)

ON CONFLICT (nombre) DO UPDATE SET
    tipo_material       = EXCLUDED.tipo_material,
    es_comercializable  = EXCLUDED.es_comercializable;

-- ------------------------------------------------------------------
-- 4) Fuente de proceso (usada para acreditar las salidas de una
--    transformación con el tipo PROCESO_SELECCION)
-- ------------------------------------------------------------------
INSERT INTO fuentes_origen (nombre, tipo_fuente) VALUES
    ('Proceso seleccion',      'PROCESO_SELECCION'),
    ('Proceso transformacion', 'PROCESO_SELECCION')
ON CONFLICT (nombre) DO UPDATE SET
    tipo_fuente = EXCLUDED.tipo_fuente;

COMMIT;

-- ============================================================
-- NOTAS
-- ============================================================
-- * La función RPC registrar_lote_inventario se reutiliza tal cual; en la
--   transformación nueva la merma se mueve como stock vendible del material
--   MERMA (Basura/Tierra), por lo que se pasa p_mermas = [] y un movimiento
--   positivo de TRANSFORMACION sobre el material de merma.
-- * Si en tu BD `materiales.nombre` no tiene restricción única, revisa los
--   duplicados ANTES de ejecutar o crea el índice único:
--     CREATE UNIQUE INDEX IF NOT EXISTS uq_materiales_nombre ON materiales (nombre);
-- ============================================================
