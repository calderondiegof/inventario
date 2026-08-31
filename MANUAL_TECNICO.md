# Manual Técnico — Agente de Inventario por WhatsApp

> **Versión:** 1.0 · **Framework:** FastAPI + Uvicorn · **Base de datos:** Supabase (PostgreSQL) · **IA:** DeepSeek · **Despliegue:** Render

---

## 1. Arquitectura General

```
WhatsApp (Meta Cloud API)
        |
        v
  FastAPI /webhook  <--- Firma HMAC-SHA256 (X-Hub-Signature-256)
        |
        v
  main.py (lifespan + deduplicación TTL)
        |
        v
  handlers/router.py  <--- Dispatch central
        |---> clientes_handler | conductores_handler
        |---> materiales_handler | remisiones_handler
        |---> consultas_handler
        v
  services/inventario_service.py
  (InventarioServiceConValidacion)
        |---> conductor_service.py | currency_service.py
        |---> enums.py | types.py
        v
  core/whatsapp.py  <--- Envío de mensajes, imágenes, PDFs
        v
  Supabase (PostgreSQL + Storage)
```

### 1.1 Stack tecnológico

| Componente | Librería / Servicio |
|---|---|
| Servidor HTTP | FastAPI + Uvicorn |
| Cliente HTTP | httpx (async) |
| Base de datos | Supabase (PostgreSQL via PostgREST) |
| Blob Storage | Supabase Storage (PDFs, imágenes) |
| Agente IA | DeepSeek API |
| Canal | WhatsApp Business Cloud API v18.0 |
| Zona horaria | `zoneinfo.ZoneInfo("America/Bogota")` |

---

## 2. Variables de Entorno (.env)

```bash
# Supabase
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGci...

# WhatsApp Business API
WHATSAPP_TOKEN=EAAGlCqZC...
PHONE_NUMBER_ID=1234567890
META_APP_SECRET=xxxxxxxxxxxxxxxx

# IA (DeepSeek)
DEEPSEEK_API_KEY=sk-deepseek-...

# Seguridad
VERIFY_TOKEN=mi_token_secreto_webhook

# Servidor
PORT=10000
PUBLIC_BASE_URL=https://miapp.onrender.com
```

> **SEGURIDAD:** `META_APP_SECRET` es obligatorio. Sin él, el servidor **rechaza todos los webhooks** (HTTP 403).

---

## 3. Endpoints HTTP

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Health check |
| `GET` | `/test` | Verifica que las variables de entorno estén configuradas |
| `GET` | `/debug` | Diagnóstico con tokens truncados |
| `GET` | `/webhook` | Verificación del webhook (Meta envía hub.verify_token) |
| `POST` | `/webhook` | Recibe eventos de WhatsApp (valida HMAC-SHA256) |
| `GET` | `/download/{archivo}` | Descarga un PDF del temp del servidor |

### 3.1 Flujo de verificación del webhook

1. Meta hace `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`
2. El servidor compara `hub.verify_token` con `VERIFY_TOKEN`
3. Si coincide, devuelve `hub.challenge` como texto plano (HTTP 200)
4. Si no coincide, devuelve HTTP 403

### 3.2 Flujo de recepción de mensajes

1. Meta hace `POST /webhook` con JSON
2. Se valida `X-Hub-Signature-256` contra `META_APP_SECRET` (HMAC-SHA256)
3. Se parsea el JSON y se dispatcha con `BackgroundTasks`
4. Se devuelve `EVENT_RECEIVED` (HTTP 200) inmediatamente



---

## 4. Modelo de Datos (Supabase / PostgreSQL)

### 4.1 Tablas

**usuarios** — `id` (PK), `nombre`, `telefono_whatsapp`, `bodega_asignada_id` (FK), `contexto_operacion` (JSONB)

**bodegas** — `id` (PK), `nombre`, `pais`, `moneda`

**materiales** — `id` (PK), `nombre`, `tipo_material`, `es_comercializable`

**fuentes_origen** — `id` (PK), `nombre`, `tipo_fuente`

**conductores** — `id` (PK), `nombre`, `identificacion`, `placa`, `placa_trailer`, `telefono`, `direccion`

**clientes** — `id` (PK), `nombre`, `identificacion`, `telefono`, `direccion`

**movimientos_inventario** — `id` (PK), `bodega_id`, `usuario_id`, `material_id`, `tipo_transaccion`, `cantidad_kg`, `precio_unitario`, `fecha_operacion`, `created_at`, `fuente_id`, `cliente_id`, `conductor_id`, `numero_remision`, `estado`, `remision_id` (FK self)

**ordenes_salida** — `id` (PK), `numero_remision`, `bodega_id`, `cliente_id`, `conductor_id`, `estado`, `created_at`

### 4.2 Tipos de material (enum)

| Valor | Significado |
|---|---|
| `BRUTO` | Material sin clasificar (revuelto) |
| `SEMILIMPIO` | Proceso parcial |
| `LIMPIO` | Clasificado y vendible |
| `MERMA` | Basura, tierra, desperdicio |

### 4.3 Tipos de transacción (enum)

| Valor | Uso |
|---|---|
| `COMPRA` | Entrada de material |
| `ENTRADA_BRUTA` | Entrada de revuelto |
| `VENTA` | Salida por venta |
| `TRANSFORMACION` | Proceso de selección/arreglo |
| `DESPACHO` | Entrega salida sin precio |
| `AJUSTE_INVENTARIO` | Corrección manual |
| `ANULACION` | Reverso de una operación |

### 4.4 Estados de remisión

| Estado | Significado |
|---|---|
| `ORDEN_SALIDA` | Venta creada, pendiente de aprobación |
| `APROBADA` | Con precios confirmados y PDF generado |
| `ANULADA` | Operación invertida, no contabiliza |

### 4.5 RPC (procedimientos almacenados)

| RPC | Descripción |
|---|---|
| `registrar_lote_inventario` | Inserta lote atómico de movimientos |
| `registrar_venta_multiple` | Orden de salida + movimientos |
| `buscar_cliente_existente` | Por identificación o teléfono |
| `registrar_cliente` | Crea cliente nuevo |
| `registrar_conductor` | Crea conductor nuevo |

---

## 5. Arquitectura de Handlers

### 5.1 Flujo de un mensaje

```
WhatsApp webhook
        |
        v
procesar_webhook (main.py)
  |-- deduplicación TTL (10 min, 5000 entries)
  v
procesar_un_mensaje (router.py)
  |-- Comandos directos (1=menu, 2=inventario, 3=anular)
  |-- Trigger de intención (DeepSeek o parser local)
  v
Handler específico
  |-- Paso a paso (contexto en usuarios.contexto_operacion)
  |-- Confirmación y registro en Supabase
```

### 5.2 Mapa de handlers

| Handler | Responsabilidad |
|---|---|
| `router.py` | Dispatch central, menús, flujo IA |
| `remisiones_handler.py` | Wizard Entrada/Salida, valorización, aprobación, anulación/corrección |
| `clientes_handler.py` | Registrar cliente (bloque o paso a paso) |
| `conductores_handler.py` | Registrar conductor (bloque/paso, dirección opcional) |
| `materiales_handler.py` | Crear materiales en el catálogo |
| `consultas_handler.py` | Inventario total, reporte diario, gráfico, historial |

---

## 6. Servicio de Inventario (InventarioServiceConValidacion)


### 7.4 whatsapp_utils.py

| Función | Descripción |
|---|---|
| `construir_lista_texto_whatsapp(items, titulo)` | Mensaje de texto numerado (>10 items) |
| `construir_seccion_lista_interactiva(filas, titulo)` | Payload de lista interactiva WhatsApp |
| `resolver_entrada_material(texto, nombres)` | Resuelve número → nombre del material |
| `es_lista_materiales(texto)` | Detecta si el texto parece lista con kilos |
| `parsear_edicion_precio(texto)` | Parsing de comando `'N VALOR'` |
| `procesar_precio_paso_a_paso(...)` | Máquina de estados para capturar precios uno a uno |
| `formatear_resumen_precios(items, precios)` | Genera resumen para confirmación |
| `borrador_para_nueva_lista(borrador, modo)` | Reinicia borrador al comenzar nueva lista |

### 7.5 parsing_utils.py

| Función | Descripción |
|---|---|
| `parsear_bloque_persona(texto)` | Extrae nombre, doc, telefono, direccion, placa(s) |
| `aplicar_sinonimos(texto)` | Mapeo informal → nombre formal del catálogo |
| `aplicar_frases(texto)` | Subraya frases compuestas con `_` |
| `normalizar_nombre_material(texto)` | Sinonimos + frases aplicados |
| `_verificar_duplicada(huella)` | Check de duplicado SHA256 |
| `_registrar_huella(huella)` | Registra operación en memoria |
| `_huella_operacion(*partes)` | Genera SHA256 de `(fecha, bodega, material)` |

### 7.6 whatsapp_formatter.py

| Función | Descripción |
|---|---|
| `formatear_movimientos_material(resultado)` | Genera texto con historial de movimientos |
| `_formatear_ficha_cliente(p)` | Ficha formateada del cliente |
| `_formatear_ficha_conductor(p)` | Ficha formateada del conductor con trailer |

---

## 8. Servicio de Moneda (currency_service.py)

```python
tasa = await obtener_tasa_dolar(pais="Colombia", moneda="COP")
tasa = await obtener_tasa_dolar(pais="Argentina", moneda="ARS")
```

| Pais / Moneda | Endpoint | Campo |
|---|---|---|
| Argentina / ARS | `dolarapi.com/v1/dolares/blue` | `(compra + venta) / 2` |
| Otros / cualquier moneda | `open.er-api.com/v6/latest/USD` | `rates[<CODIGO_ISO>]` |

Si la red falla o los datos están incompletos, devuelve `None` (no lanza excepciones). La moneda se infiere del país si no se pasa explícitamente.


## 11. Agente IA — DeepSeek (core/modelos_ia.py)

### 11.1 Pipeline

1. `inferir_datos_ia(...)` → construye prompt dinámico con catálogo + borrador + fecha
2. `llamar_deepseek(prompt, texto)` → devuelve JSON estructurado
3. `fusionar_borrador(borrador, respuesta_ia)` → combina datos del wizard con IA
4. `validar_completitud(datos, fecha)` → verifica campos obligatorios por intención

### 11.2 Modelo de respuesta (Pydantic)

```python
class RespuestaAgente(BaseModel):
    intencion: Literal[
        "REGISTRO_DIARIO", "ENTRADA_REVUELTO", "SELECCION_REVUELTO",
        "TRANSFORMACION_MATERIAL", "COMPRA_DIRECTA", "VENTA_DESPACHO",
        "AJUSTE_INVENTARIO", "CONSULTA", "CONSULTA_INVENTARIO_TOTAL",
        "VER_MOVIMIENTOS_SELECCION", "REPORTE_POR_FECHA", "OTRO"
    ]
    items, entradas_revuelto, cliente, cliente_conductor
    material_origen, merma_kg, nombre_proceso, fuente_compra
    fecha_operacion, respuesta_texto
```

Si la IA coloca campos en la intención equivocada, `fusionar_borrador` redistribuye los datos al destino correcto.

---

## 12. Seguridad

| Mecanismo | Detalle |
|---|---|
| Firma HMAC-SHA256 | `X-Hub-Signature-256` validada contra `META_APP_SECRET` |
| `META_APP_SECRET` obligatorio | Si no está, HTTP 403 sin procesar nada |
| `VERIFY_TOKEN` | Para la verificación inicial del webhook |
| `hmac.compare_digest` | Comparación en tiempo constante |
| `clean_payload()` | Elimina `None` recursivamente antes de enviar a Meta |

---

## 13. Manejo de Errores

| Situación | Comportamiento |
|---|---|
| Red caída / timeout | Loguea warning, flujo continúa |
| Material no en catálogo | `difflib.get_close_matches` (cutoff=0.75) muestra candidatos |
| Operación duplicada | Huella SHA256: advierte al usuario |
| JSON inválido en webhook | HTTP 400 |
| Firma inválida | HTTP 403, sin procesamiento |
| Excepción no controlada | `logger.exception()` — nunca se silencia |

---

## 14. Tests

**35 tests unitarios** en 2 archivos:

| Suite | Cobertura |
|---|---|
| `test_currency_service.py` | Parsing de tasas, errores de red, Argentina/Colombia/otros |
| `test_transformaciones.py` | Conservación de masa, reglas de negocio, wizards, normalización, IA fallback |

```bash
python -m pytest tests/ -v
```

---

## 15. Despliegue

### 15.1 Render

```
Build Command:  (vacío)
Start Command:  python -m uvicorn main:app --host 0.0.0.0 --port $PORT
```

Todas las variables de `.env` se configuran en el panel de Render.

### 15.2 Configurar webhook en Meta

1. Meta for Developers → WhatsApp → Configuration
2. Callback URL: `https://miapp.onrender.com/webhook`
3. Verify Token: el valor de `VERIFY_TOKEN`
4. Suscribir la app al webhook

### 15.3 Recargar catálogos sin reiniciar

```python
from core.config import inventario
inventario.recargar_catalogos()
```

---

## 16. Diagrama de Secuencia — Registro de Venta

```
Usuario          Router          Remisiones          Inventario          WhatsApp
  |                 |                 |                   |                  |
  |-- 1 ------------>|                 |                   |                  |
  |<-- Menu --------|                 |                   |                  |
  |-- Salida ------>|                 |                   |                  |
  |                 |-- wizard ------->                   |                  |
  |<-- Cliente? ----|                 |                   |                  |
  |-- Acero SA ---->|                 |                   |                  |
  |                 |-- guardar ------->                   |                  |
  |<-- ok ----------|                 |                   |                  |
  |-- Doc? -------->|                 |                   |                  |
  |-- 901234567 --->|                 |                   |                  |
  |                 |-- ... (8 pasos) |                   |                  |
  |-- Cobre 150 --->|                 |                   |                  |
  |                 |                   |-- registrar_ ----->                  |
  |                 |                   |   venta_multiple  |                  |
  |                 |                   |<-- remision -------|                  |
  |                 |                   |-- generar_pdf --->                  |
  |                 |                   |<-- PDF -----------|                  |
  |<--------------------------------------- PDF -------------|                  |
```

---

## 17. Glossario Técnico

| Término | Significado |
|---|---|
| `inventario` | Instancia de `InventarioServiceConValidacion` |
| `contexto_operacion` | JSONB en `usuarios` que guarda el estado del wizard activo |
| `borrador_pendiente` | Clave del contexto que acumula items/precios durante el registro |
| `intencion` | Tipo de operación interpretada por DeepSeek o por el router |
| `huella SHA256` | Hash de `(fecha, bodega, material)`: evita duplicados silenciosos |
| `RPC` | Remote Procedure Call — procedimiento almacenado en Supabase |
| `TipoMaterial` | Enum: BRUTO, SEMILIMPIO, LIMPIO, MERMA |
| `TipoTransaccion` | Enum: COMPRA, VENTA, TRANSFORMACION, DESPACHO, AJUSTE, ANULACION |
| `ORDEN_SALIDA` | Estado transitorio: venta sin precios aprobados |
| `APROBADA` | Estado final: remisión con PDF |
| `ANULADA` | Estado terminal: movimientos invertidos |

---

## 9. Envío de Mensajes (core/whatsapp.py)

| Función | Tipo |
|---|---|
| `enviar_mensaje_whatsapp(destino, texto)` | Texto plano |
| `enviar_botones_whatsapp(destino, texto, botones)` | Botones (máx 3) |
| `enviar_lista_whatsapp(destino, texto, filas)` | Lista interactiva (máx 10 filas) |
| `enviar_imagen_whatsapp(destino, url, leyenda)` | Imagen con leyenda |
| `enviar_documento_whatsapp(destino, ruta, nombre)` | PDF con filename |

### 9.1 Regla de 10 filas

Si el catálogo tiene más de 10 materiales, se envía un mensaje de texto ordenado alfabéticamente en lugar de una lista interactiva (evita el error 131009 de Meta).

### 9.2 Upload de PDFs

1. Intenta subir a Supabase Storage (`documentos/{timestamp}_{nombre}`)
2. Si falla, usa `PUBLIC_BASE_URL/download/{archivo}` como fallback

---

## 10. Deduplicación de Mensajes (core/contexto.py)

```python
_mensajes_whatsapp_procesados: Dict[str, float]  # mensaje_id -> timestamp
_MAX_MENSAJES_PROCESADOS = 5000
_MENSAJE_TTL_SEGUNDOS = 600.0  # 10 minutos
```

- **TTL de 10 minutos:** Meta puede reintentar webhooks. Evita operaciones duplicadas.
- **Purga incremental:** solo se borran las entradas expiradas.
- **Límite de 5000 entries:** si se supera, se limpia todo.

### 6.1 Métodos públicos

| Método | Descripción |
|---|---|
| `recargar_catalogos()` | Recarga materiales y fuentes desde Supabase |
| `registrar_entrada_agrupada(...)` | Registra múltiples fuentes de revuelto en lote |
| `registrar_seleccion_revuelto(...)` | Descuenta revuelto, crea materiales resultantes |
| `registrar_transformacion_material(...)` | Transforma un material con merma |
| `registrar_compra_directa(...)` | Entrada directa de materiales clasificados |
| `registrar_ajuste_inventario(...)` | Ajuste manual positivo o negativo |
| `registrar_venta_multiple(...)` | Crea orden de salida + movimientos |
| `aprobar_orden_salida(...)` | Confirma precios, genera número de remisión |
| `anular_o_corregir_remision(...)` | Anula o corrige una remisión |
| `construir_mensaje_seleccion(...)` | Genera mensaje de confirmación con omitidos |
| `buscar_cliente_existente(...)` | Búsqueda por identificación o teléfono |
| `buscar_conductor_existente(...)` | Búsqueda por identificación, placa o nombre |

### 6.2 Contexto del usuario (wizards)

El **contexto** (`usuarios.contexto_operacion`, JSONB) guarda el estado del wizard:

```json
{
  "campo_esperado": "conductor_documento",
  "accion_pendiente": { "tipo": "crear_cliente_paso", "datos": {} },
  "borrador_pendiente": { "intencion": "VENTA_DESPACHO", "items": [] }
}
```

### 6.3 Deduplicación SHA256

`_HUELLAS` (dict en memoria) con clave SHA256 de `(fecha, bodega, material, cantidad)` evita registros duplicados.

---

## 7. Módulos de Utils (fuente única de verdad)

### 7.1 text_normalizer.py

| Función | Descripción |
|---|---|
| `normalizar(texto)` | Quita tildes, diacríticos, colapsa espacios → minúsculas |
| `normalizar_digitos(texto)` | Solo dígitos (teléfono, documento) |
| `normalizar_placa(placa)` | Mayúsculas, sin guiones ni espacios |
| `extraer_placas(texto)` | Detecta hasta 2 placas (cabezal + trailer) |
| `_normalizar_texto` | Alias interno para `parsers.py` |
| `normalizar_nombre` | Alias de `normalizar` |

### 7.2 number_parser.py

| Función | Descripción |
|---|---|
| `_parsear_numero(texto)` | `'4.120,50'` → `4120.5`, `'1.250.000'` → `1250000` |
| `_parsear_numero_moneda` | Alias público de `_parsear_numero` |

### 7.3 parsers.py

| Función | Descripción |
|---|---|
| `parsear_material_cantidad(texto)` | `'Cable 120'` → `('Cable', 120.0)` |
| `parsear_campos_cliente(texto)` | Bloque clave:valor para cliente |
| `parsear_campos_cliente_venta(texto)` | Bloque conductor para venta |
| `parsear_fecha_colombiana(texto)` | `'hoy'`, `'25-08-2026'` → ISO |
