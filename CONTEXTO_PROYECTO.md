# CONTEXTO DEL PROYECTO — Agente de Inventario (chatarra/reciclaje) por WhatsApp

> Documento para retomar el trabajo en un chat nuevo. Arquitectura, decisiones,
> estado actual y pendientes. Resumido y ACTUALIZADO.

## 1. Qué es
Bot de WhatsApp (FastAPI + Python) que gestiona inventario de materiales de
reciclaje/chatarra. Un agente IA (DeepSeek) clasifica los mensajes en
"intenciones" y el servicio de negocio los valida/guarda en Supabase.

- Plataforma: **Windows**. Python 3.13.
- Dependencias: fastapi, uvicorn, httpx, supabase, python-dotenv, pandas,
  matplotlib, reportlab.

## 2. Estructura REAL (migración a handlers ya hecha)
```
main.py                          # FastAPI: webhook WhatsApp + delega a handlers/router
handlers/router.py               # Orquestador: lee mensaje, estado usuario, delega
handlers/remisiones_handler.py   # Wizard registro Entrada/Salida + anular/corregir + valoración
handlers/consultas_handler.py    # Inventario total, movimientos, reporte diario, gráfico
handlers/clientes_handler.py     # Crear/editar cliente (módulo unificado)
handlers/conductores_handler.py  # Crear/editar conductor
handlers/materiales_handler.py   # Crear material
handlers/pdf_handler.py          # Comando pdf: impresión/reimpresión/listado
core/config.py                   # Env, supabase, inventario, http_client, constantes
core/modelos_ia.py               # RespuestaAgente + prompt_agente + inferir_datos_ia
core/whatsapp.py                 # Envío WhatsApp (texto, botones, listas, doc, imagen)
core/contexto.py                 # guardar_contexto, dedup TTL de mensajes
utils/parsers.py                 # parsear_fecha, VENTA_CAMPOS_PASO, cliente_venta...
utils/whatsapp_utils.py          # procesar_precio_paso_a_paso, formatear_resumen
services/inventario_service.py   # Toda la lógica de negocio / inventario
generador_pdf.py                 # PDF de remisión (REM_MODELO)
reporte_grafico.py               # Gráfico/dashboard de stock
tests/                           # pytest (40 tests, todos pasan)
```
`main_old.py` y `services/inventario_service_old.py` = legacy de referencia
(NO usados por el bot). `_fix.py`, `fxpdf.py`, `*.bak` = temporales/descartables.

## 3. Flujo de un mensaje (router.py)
En `procesar_un_mensaje(message, contactos)`:
1. Extrae texto (type/text, interactive button/list) y lo normaliza.
2. Consulta `usuarios` por `telefono_whatsapp` → `usuario_id`, `bodega_id`,
   `contexto_operacion`.
3. "cancelar/limpiar/reiniciar" → limpia contexto.
4. Si hay `accion_pendiente.tipo` → delega a su handler (corrección,
   reporte_fecha, crear_cliente/conductor/material, valoración OS).
5. Saludos → bienvenida. Comandos directos → menús/pdf/OS.
6. **FALBACK al wizard de registro:** sin contexto activo se llama
   `remisiones_handler.procesar_wizard_registro(...)` con TODOS los kwargs.
   Este método continúa un `campo_esperado` pendiente O llama a DeepSeek
   (`inferir_datos_ia`) para interpretar texto nuevo.
7. `main.py` envía por WhatsApp cualquier `str` que el router devuelva (safety
   net; el wizard envía su propia respuesta y retorna MANEJADO/None).

## 4. Intenciones (RespuestaAgente)
```
REGISTRO_DIARIO | ENTRADA_REVUELTO | SELECCION_REVUELTO | TRANSFORMACION_MATERIAL
| COMPRA_DIRECTA | VENTA_DESPACHO | AJUSTE_INVENTARIO | CONSULTA |
CONSULTA_INVENTARIO_TOTAL | VER_MOVIMIENTOS_SELECCION | REPORTE_POR_FECHA | OTRO
```
Campos: `fecha_operacion, entradas_revuelto, items, cantidad_revuelto_procesada,
merma_kg, material_origen, material_merma, nombre_proceso, fuente_compra,
cliente, cliente_documento, cliente_direccion, cliente_placa, cliente_conductor,
cliente_conductor_id, cliente_celular, cliente_conductor_celular,
consulta_material, respuesta_texto`.

## 5. Modelo de estados material
- **BRUTO (A)** = Revuelto (Cooperativa/Pesca/Planta/Corrientes/Compra).
- **SEMILIMPIO (B)** = Arreglo Cobre y Bronce, Arreglo Aluminio, Arreglo Carter,
  Cable, Arreglo Antimonio, Bobinas, Plastico, Caucho, Cable Quemado, Arreglo Dificil.
- **LIMPIO (C)** = Bronce, Cobre, Carter, Lamina, Antimonio, Plomo, Chatarra,
  Resistencia, Radiador Cobre/Bronce, Radiador, Manguera, Tarjeta, Acero, Olla,
  Perfil, Zinc, Clausen, Baterias.

## 6. FLUJO DE VENTA (registro → confirmación → valoración de precios)
**A) Registrar la venta (usuario operador):**
1. Escribe p.ej. `venta cobre 1 arreglo carter 1 olla 2` (sin contexto activo).
2. Router → `procesar_wizard_registro` → DeepSeek interpreta `VENTA_DESPACHO`.
3. El wizard pregunta uno a uno los campos faltantes (fecha, cliente, conductor;
   `VENTA_CAMPOS_PASO`). Validación con `validar_completitud`.
4. `inventario.registrar_venta_multiple(...)` → crea remisión `REM_xxx` estado
   `ORDEN_SALIDA` y genera el PDF.
5. **Confirmación visual** (lista cada material):
   ```
   ✅ Orden de Salida #REM_117 registrada exitosamente:
      • Cobre: 1.00 kg
      • Arreglo carter: 1.00 kg
   📅 Fecha: 2026-08-31
   ⏳ Pendiente de valoración y aprobación por Contabilidad.
   ```

**B) Valoración/despacho (usuario Contabilidad):**
1. "ordenes de salida" → `iniciar_aprobacion_orden_salida` lista ORDEN_SALIDA.
2. Elige la orden → `preparar_flujo_valorizacion` (sugiere tasa dólar).
3. Fija `vr_dolar_dia` → entra a captura de precios.
4. **Precios 1x1:** `captura_precio_material` llama `procesar_precio_paso_a_paso`
   y pregunta el precio de CADA material (flujo: pregunta 1 → captura → pregunta 2
   → ... → resumen). `0` descarta el precio anterior; `[n] [precio]` edita en resumen.
5. `resumen_precios` → OK/SI aprueba (RPC `aprobar_remision_con_precios`),
   o `0`/CANCELAR anula.
6. `seleccion_modo_pdf` → MONEDA_LOCAL/DOLARES/AMBAS/SIN_VALORES → genera PDF
   aprobado y lo envía. **Al aprobar se limpia `accion_pendiente` para que un
   "hola" posterior no quede atrapado en el paso de moneda.**

## 7. Manejo de errores clave de venta
- **"Stock insuficiente de X. Disponible: 0.00; requerido: 2.00"** → la validación
  falla, se purga el borrador (cierra el wizard automáticamente), y el bot responde
  un aviso claro: revisar con `ver inventario` y reenviar la venta corregida.
  NO hace falta escribir "cancelar". La huella se graba solo si el guardado
  es exitoso (sin falsos duplicados en reintento).

## 8. Otros flujos
- **Transformación:** `registrar_transformacion_material`, conservación de masa:
  `cantidad_procesada == Σ salidas + merma`. Regla 1 BRUTO→, Regla 2 re-transformación
  SEMILIMPIO, Regla 3 selección técnica (Arreglo Carter → Carter + chatarra + merma).
- **Anular/Corregir rem:** wizard en `procesar_flujo_remision` (espera_remision_modo
  → numero → alcance → opciones material/cliente → `regenerar_y_enviar_pdf_remision`).
- **PDF:** comando `pdf`, `rem_XXX`/`remisión XXX`, `remisiones`.

## 9. Pruebas
- Compilar: `python -m py_compile main.py handlers/*.py core/*.py`
- Tests: `python -m pytest tests/ -q` → **40 passed** (test_currency_service,
  test_pdf_remision_service, test_transformaciones). Ignorar `test_vistas.py`
  (script con `exit()` que rompe la colección).

## 10. Estilo y notas IMPORTANTES
- Sin tildes en identificación normalizada: `normalizar()` en service.
- Sinónimos: `grueso→carter`, `rechazo→arreglo`.
- Toda escritura termina en `_guardar_lote()` → RPC `registrar_lote_inventario`.
- Errores con `ValueError` + mensajes en español.
- `asyncio.to_thread(...)` para todas las llamadas síncronas del servicio.
- **Shadowing:** el directorio local `supabase/` (migraciones SQL) ensombrece al
  paquete pip `supabase`. `core/config.py` ya lo maneja sacándolo de sys.modules.
- **Despliegue:** push a `origin/main`; Render redeploy (auto o Manual Deploy).

## 11. Pendientes / sugerencias (NO aplicadas)
- Corregir también el conductor en el modo Corrección.
- Regenerar/notificar algo tras ANULAR una remisión completa.
- Limpiar del repo los archivos legacy/temporales: `main_old.py`,
  `services/inventario_service_old.py`, `_fix.py`, `fxpdf.py`, `*.bak`,
  `test_vistas.py`, `test_helper.py`.
- **MERMA (D)** = Tierra, Basura (Ahora CON STOCK VENDIBLE, `es_comercializable=true`).