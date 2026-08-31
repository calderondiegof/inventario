-- ============================================================================
-- Migración: placa_trailer (remolque) en conductores
-- Soporta la placa/patente OPCIONAL del remolque del conductor
-- (flujo dual 'Placa / Patente' + 'Placa Trailer' del bot de WhatsApp).
-- Idempotente: safe de re-ejecutar.
-- ============================================================================
BEGIN;

ALTER TABLE conductores
    ADD COLUMN IF NOT EXISTS placa_trailer TEXT;

COMMENT ON COLUMN conductores.placa_trailer IS
    'Patente del remolque/trailer (opcional). La placa del camión va en placa.';

COMMIT;
