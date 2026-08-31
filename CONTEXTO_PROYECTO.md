# CONTEXTO DEL PROYECTO — Bot de Inventario (chatarras) por WhatsApp

> Retomar trabajo en chat nuevo. FASTAPI + Python 3.13 + DeepSeek + Supabase.

## Arquitectura (real, migrada a handlers)
```
main.py                      # webhook WhatsApp → delega a handlers/router
handlers/router.py           # orquestador: tipo msg + estado user + delega
handlers/remisiones_handler.py  # ★wizard registro Entrada/Salida + anular/corregir + valoración precios
handlers/consultas_handler.py   # inventario total, movimientos, reporte diario
handlers/{clientes,conductores,materiales}_handler.py  # creaciones
handlers/pdf_handler.py         # cmd pdf / rem_XXX / remisiones
core/{config,modelos_ia,whatsapp,contexto}.py   # env, RespuestaAgente+IA, envío, contexto
utils/{parsers,whatsapp_utils,text_normalizer,...}.py
services/inventario_service.py # ★toda la lógica de negocio/RPC
generador_pdf.py / reporte_grafico.py   # PDF remisión / gráfico stock
tests/                        # pytest → 40 test OK
```
Legacy NO usado por el bot: `main_old.py`, `services/inventario_service_old.py`,
`_fix.py`, `fxpdf.py`, `*.bak`.

## Flujo por mensaje (router.procesar_un_mensaje)
1 extrae+normaliza texto → 2 carga usuario por teléfono (usuario_id,bodega_id,contexto)
3 "cancelar" limpia → 4 si `accion_pendiente.tipo` delega a su handler
5 saludos/comandos directos → 6 SIN contexto: `remisiones_handler.procesar_wizard_registro(...)`
  (continúa `campo_esperado` O llama DeepSeek `inferir_datos_ia` para texto nuevo)
7 main.py envía el `str` que devuelva el router (safety net).

## Intenciones IA (RespuestaAgente)
REGISTRO_DIARIO|ENTRADA_REVUELTO|SELECCION_REVUELTO|TRANSFORMACION_MATERIAL|
COMPRA_DIRECTA|VENTA_DESPACHO|AJUSTE_INVENTARIO|CONSULTA|CONSULTA_INVENTARIO_TOTAL|
VER_MOVIMIENTOS_SELECCION|REPORTE_POR_FECHA|OTRO
Campos: fecha_operacion,entradas_revuelto,items,cantidad_revuelto_procesada,
merma_kg,material_origen,material_merma,nombre_proceso,fuente_compra,cliente,
cliente_documento,direccion,placa,cliente_conductor,id,celulares,
consulta_material,respuesta_texto.

## Estados material
BRUTO(A)=Revuelto | SEMILIMPIO(B)=Arreglos/Cable/Plastico/etc | LIMPIO(C)=Cobre,Bronce,Carter,etc
| MERMA(D)=Tierra/Basura (**stock vendible** es_comercializable=true).

## ★ FLUJO VENTA
**A) Registrar (operador):** texto `venta cobre 1 ...` → DeepSeek VENTA_DESPACHO
→ wizard pide campos faltantes (fecha,cliente,conductor; VENTA_CAMPOS_PASO;
validar_completitud) → `inventario.registrar_venta_multiple` crea REM estado
ORDEN_SALIDA + PDF → confirmación visual lista cada material/kilo.
**B) Valorar (Contabilidad):** "ordenes de salida" → `iniciar_aprobacion_orden_salida`
→ elige orden → `preparar_flujo_valorizacion` (tasa dólar) → fija `vr_dolar_dia`
→ **precios 1x1** `procesar_precio_paso_a_paso` (pregunta CADA material; `0` descarta
anterior; `[n] precio` edita) → `resumen_precios` OK/SI aprueba RPC
`aprobar_remision_con_precios` (o 0/CANCELAR anula) → `seleccion_modo_pdf`
MONEDA_LOCAL/DOLARES/AMBAS/SIN_VALORES → PDF aprobado.
**FIX clave:** al aprobar se setea `contexto["accion_pendiente"]={}` inmediatamente
(antes era código muerto tras los return) → evita quedar pegado en "modo moneda".

## Errores venta
"Stock insuficiente" → purga borrador (cierra wizard) + aviso claro; user reenvía
la venta corregida; NO necesita "cancelar". Huella solo si guardado OK (sin
duplicados). Precios: `res["tipo"]=="continuar"` para preguntar el siguiente.

## Otros flujos
Transformación: `registrar_transformacion_material`, masa: procesada==Σsalidas+merma
(Reglas 1 BRUTO→,2 re-transform,3 selección: Arreglo Carter→Carter+chat+merma).
Anular/Corregir: `procesar_flujo_remision` (TIPO_CORRECCION) → regenera PDF con mismo nº.
PDF: cmd `pdf`, `rem_XXX`, `remisión XXX`, `remisiones`.

## Verificar
`python -m py_compile main.py handlers/*.py core/*.py` · `python -m pytest tests/ -q`→40 OK.
Ignorar test_vistas.py (rompe colección). Despliegue: push→origin/main; Render redeploy.

## Estilo
normalizar() sin tildes; sinónimos grueso→carter,rechazo→arreglo; escritura vía
_guardar_lote()→RPC registrar_lote_inventario; ValueError+español; todas las
llamadas síncronas con asyncio.to_thread. Shadowing: dir local `supabase/`
ensombrece paquete pip; core/config.py ya lo maneja.

## Pendientes (NO hechos)
- Corregir conductor en modo Corrección. · Regenerar/notif tras ANULAR completa.
- Limpiar legacy: main_old.py, inventario_service_old.py, _fix.py, fxpdf.py, *.bak,
  test_vistas.py, test_helper.py.