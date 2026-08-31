-- ============================================================
-- MIGRACIÓN: MÓDULO CONDUCTOR (espejo de CLIENTES)
-- Ejecutar en el SQL Editor de Supabase
-- El código (main.py / services/inventario_service.py) depende de
-- esta tabla y de la columna remisiones.conductor_id.
-- ============================================================

-- 1) Crear la tabla CONDUCTORES (misma forma que CLIENTES)
CREATE TABLE IF NOT EXISTS conductores (
    id             BIGSERIAL PRIMARY KEY,
    nombre         TEXT NOT NULL,
    identificacion TEXT,                   -- cédula / número ID del conductor
    telefono       TEXT,                   -- celular del conductor
    placa          TEXT,                   -- patente / placa del vehículo
    direccion      TEXT,
    creado_en      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT conductores_identificacion_uniq UNIQUE (identificacion)
);

-- Índices para búsquedas rápidas por nombre, ID y placa
CREATE INDEX IF NOT EXISTS idx_conductores_nombre         ON conductores (nombre);
CREATE INDEX IF NOT EXISTS idx_conductores_identificacion ON conductores (identificacion);
CREATE INDEX IF NOT EXISTS idx_conductores_placa          ON conductores (placa);

-- 2) Agregar conductor_id a REMISIONES (igual que cliente_id)
ALTER TABLE remisiones
    ADD COLUMN IF NOT EXISTS conductor_id BIGINT REFERENCES conductores(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_remisiones_conductor_id ON remisiones (conductor_id);

-- ============================================================
-- 3) MIGRACIÓN DE DATOS HISTÓRICOS: tomar los conductores que
--    hoy están embebidos en REMISIONES y pasarlos a CONDUCTORES.
-- ============================================================
INSERT INTO conductores (nombre, identificacion, telefono, placa)
SELECT DISTINCT
    NULLIF(TRIM(r.conductor),          ''),
    NULLIF(TRIM(r.id_conductor),       ''),
    NULLIF(TRIM(r.celular_conductor),  ''),
    NULLIF(TRIM(r.patente),            '')
FROM remisiones r
WHERE NULLIF(TRIM(r.conductor), '') IS NOT NULL
ON CONFLICT (identificacion) DO NOTHING;

-- 4) Vincular cada remisión histórica con su conductor ya creado
UPDATE remisiones r
SET conductor_id = c.id
FROM conductores c
WHERE r.conductor_id IS NULL
  AND c.identificacion = NULLIF(TRIM(r.id_conductor), '');

-- ============================================================
-- 5) (OPCIONAL) DESPUÉS de verificar la migración, eliminar los
--    campos de conductor embebidos en REMISIONES.
-- ============================================================
-- ALTER TABLE remisiones DROP COLUMN IF EXISTS patente;
-- ALTER TABLE remisiones DROP COLUMN IF EXISTS conductor;
-- ALTER TABLE remisiones DROP COLUMN IF EXISTS id_conductor;
-- ALTER TABLE remisiones DROP COLUMN IF EXISTS celular_conductor;
