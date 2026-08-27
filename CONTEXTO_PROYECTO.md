# CONTEXTO DEL PROYECTO — Agente de Inventario (chatarra/reciclaje) por WhatsApp

> Documento para retomar el trabajo en un chat nuevo. Contiene arquitectura,
> decisiones tomadas, estado actual y pendientes.

## 1. Qué es
Bot de WhatsApp (FastAPI + Python) que gestiona inventario de materiales de
reciclaje/chatarra. Entiende lenguaje natural gracias a un agente IA (DeepSeek)
que clasifica los mensajes en "intenciones" estructuradas y las guarda en Supabase.

- Plataforma: **Windows** (PowerShell). Python 3.13/3.14 presente.
- Dependencias en `requirements.txt`: fastapi, uvicorn, httpx, supabase,
  python-dotenv, pandas, matplotlib, reportlab.

## 2. Estructura del proyecto (raíz: `inventario/`)
```
main.py                      # FastAPI: webhooks WhatsApp + agente IA + enrutamiento
services/inventario_service.py  # Toda la lógica de negocio / inventario
generador_pdf.py             # Genera PDF de remisión (modelo REM_MODELO)
reporte_grafico.py           # Gráfico/dashboard de stock
supabase/migracion_conductores.sql, migracion_transformaciones.sql  # migraciones SQL
tests/test_transformaciones.py  # pruebas offline con cliente Supabase simulado
MANUAL_USUARIO.md            # manual de usuario (WhatsApp)
CONTEXTO_PROYECTO.md         # (este archivo)
assets/logo_ferroma.jpeg     # logo para el PDF
```

## 3. Stack y servicios
- **Supabase**: tablas `materiales`, `fuentes_origen`, `movimientos_inventario`,
  `mermas_proceso`, `clientes`, `conductores`, `remisiones`, `usuarios`, `bodegas`.
  RPC `registrar_lote_inventario` para inserciones atómicas de movimientos.
- **DeepSeek**: `llamar_deepseek(prompt, mensaje)` devuelve un JSON estructurado
  (modelo Pydantic `RespuestaAgente`) interpretando el texto del usuario.
- **Variables de entorno (.env o servidor):** `SUPABASE_URL`, `SUPABASE_KEY`,
  `DEEPSEEK_API_KEY`, `VERIFY_TOKEN`, `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`,
  `META_APP_SECRET`.

## 4. Intenciones (intencion) soportadas en el agente
```
REGISTRO_DIARIO | ENTRADA_REVUELTO | SELECCION_REVUELTO | TRANSFORMACION_MATERIAL
| COMPRA_DIRECTA | VENTA_DESPACHO | AJUSTE_INVENTARIO | CONSULTA | OTRO
```
Campos del modelo: `fecha_operacion`, `entradas_revuelto`, `items`,
`cantidad_revuelto_procesada`, `merma_kg`, `material_origen`, `material_merma`,
`nombre_proceso`, `fuente_compra`, campos de cliente/conductor,
`consulta_material`, `respuesta_texto`.

## 5. Modelo de estados de material (4 ESTADOS)
- **BRUTO (A)** = Revuelto (fuentes: Cooperativa/Pesca/Planta/Corrientes/Compra).
- **SEMILIMPIO (B)** = Arreglo Cobre y Bronce, Arreglo Aluminio, Arreglo Carter,
  Cable, Arreglo Antimonio, Bobinas, Plastico, Caucho, Cable Quemado, Arreglo Dificil.
- **LIMPIO (C)** = Bronce, Cobre, Carter, Lamina, Antimonio, Plomo, Chatarra,
  Resistencia, Radiador Cobre/Bronce, Radiador, Manguera, Tarjeta, Acero, Olla,
  Perfil, Zinc, Clausen, Baterias.
- **MERMA (D)** = Tierra, Basura. (Antes DESPERDICIO; se reclasifica a MERMA).

En el servicio: enum `TipoMaterial` (BRUTO/SEMILIMPIO/LIMPIO/MERMA + compat
DESPERDICIO). Al cargar catálogo: `PROCESABLE→SEMILIMPIO` y `DESPERDICIO→MERMA`.
**La MERMA ahora tiene stock vendible** (`es_comercializable=true` para Basura/Tierra).

## 6. Transformación de materiales (lógica VIGENTE)
Método central: **`registrar_transformacion_material`** en el servicio, con
conservación de masa: `cantidad_procesada == Σ salidas + merma` (valida stock
suficiente y que ningún producto sea BRUTO).

- Regla 1 - Transformación primaria (BRUTO→limpio/semilimpio/merma): origen solo
  "Revuelto".
- Regla 2 - Re-transformación (SEMILIMPIO→SEMILIMPIO+merma, ej. quema de Cable).
- Regla 3 - Selección técnica (SEMILIMPIO→LIMPIO+SEMILIMPIO+merma, ej. Arreglo Carter).
- La merma se acredita como movimiento positivo al material MERMA si existe en
  el catálogo; si no, cae a `mermas_proceso` (sin stock).

## 7. Venta / despacho
`registrar_venta_multiple` permite vender TODOS los materiales (bruto,
semilimpio, limpio y merma) con `es_comercializable=true` y stock suficiente.
Genera **remisión** y **PDF** (`generar_remision_pdf_archivo`).
## 8. Menú y flujo de WhatsApp (main.py)
Menú principal (por texto o botones):
| Opción | Acepta |
|--------|--------|
| 1 - Ingresar Inventario | `1`, `Ingresar Inventario` |
| 2 - Ver Inventario | `2`, `inventario`, `saldos`, `ver saldos` |
| 3 - Anular/Corregir Rem | `3`, `Anular Inventario`, `anular`, `corregir`, `anular/corregir rem`, `anular rem`, `corregir rem` |

Comandos directos: `reporte hoy/ayer`, `ver grafico`, `movimientos`,
`corregir cliente`, `anular`/`corregir`.

### Opción 3 (anular/corregir remisión) — estado actual
Estados en `procesar_un_mensaje` (accion_pendiente):
1. `espera_remision_modo` → pregunta **ANULAR o CORREGIR**.
2. `espera_numero_remision` (modo: anular / corregir).
3. `espera_alcance` (anular toda sí/no; "no" → corregir).
4. `corregir_opciones` (1.Material 2.Cliente 3.Finalizar).
5. `espera_material` → `espera_confirmacion_actualizacion`.
6. `correccion_rem_cliente` (edita cliente).
7. `finalizar`/`3` → **`regenerar_y_enviar_pdf_remision(...)`**: regenera el PDF
   con el MISMO número de remisión (nunca llama `generar_numero_remision`) y lo envía.

Servicio nuevo: `inventario.obtener_datos_pdf_remision(numero)` reúne remisión,
cliente, conductor e items (positivos); lanza `ValueError` si la remisión no existe.

## 9. Pruebas y validación (cómo verificar)
- Compilación: `python -m py_compile main.py services\inventario_service.py`
- Tests offline (23 pruebas, TODAS PASAN):
  `python -m tests.test_transformaciones`
  Incluye: Regla 1/2/3, conservación de masa, stock insuficiente, venta de merma,
  y `test_regenerar_pdf_datos`.

## 10. Pendientes / notas IMPORTANTES (para no reiniciar de cero)
1. **Problema de shadowing:** el directorio local `supabase/` (migraciones SQL)
   **ensombrece al paquete pip `supabase`** al importar desde la raíz
   (`from supabase import Client` falla: "unknown location"). Afecta a `main.py`
   en runtime. Recomendado: **renombrar `supabase/` a `sql/` o `migraciones/`**.
   En tests se inyecta un stub (`sys.modules["supabase"]`) para importar offline.
2. **Caso B de la documentación original (Carter) suma 1050 kg de salida vs
   1000 kg de entrada** (500+200+50+50+250). El sistema lo rechaza por
   conservación de masa. En el test se usa 200 kg de merma para cuadrar a 1000.
3. La MERMA pasó a ser stock vendible (cambio de comportamiento decidido).
4. **No validado en ambiente real:** migración SQL en Supabase, flujo real con
   DeepSeek, ni envío real de PDF (requiere credenciales/env con valores).
5. Sugerencias opcionalmente pendientes (NO aplicadas aún):
   - Corregir también el conductor en el modo Corrección.
   - Regenerar/notificar algo tras ANULAR una remisión completa.

## 11. Reglas de estilo (importante)
- Sin tildes en identificación normalizada: `normalizar()` en service.
- Sinónimos de material: `grueso→carter`, `rechazo→arreglo`.
- Todo método de negocio de escritura termina llamando a `_guardar_lote()`, que
  invoca la RPC `registrar_lote_inventario`.
- Manejo de errores con `ValueError` + mensajes en español para WhatsApp.
- `main.py` usa `asyncio.to_thread(...)` para todas las llamadas síncronas del
  servicio de inventario.