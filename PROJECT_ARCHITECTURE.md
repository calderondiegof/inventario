# PROJECT_ARCHITECTURE — Agente de Inventario (FERROMA) por WhatsApp

> Documento de arquitectura técnica para un desarrollador senior. Describe el
> diseño **actual** del sistema para poder hacer cambios sin romper la estructura
> existente. Generado a partir del código fuente real (2026-08-27).
> Complemento técnico de `CONTEXTO_PROYECTO.md` y `MANUAL_USUARIO.md`.

---

## 1. Resumen Ejecutivo y Propósito

### 1.1 Propósito

**Bot conversacional de WhatsApp** que gestiona el inventario físico de una
recicladora/chatarra de materiales (FERROMA). Un usuario autorizado escribe en
lenguaje natural (ej. *"Cooperativa 500"*, *"Quemar 1000 de Cable"*) y el bot:

1. **Interpreta** la intención con un LLM (DeepSeek) → JSON estructurado.
2. **Valida** reglas de negocio (conservación de masa, stock, estados de material).
3. **Persiste** movimientos de inventario de forma **atómica** en Supabase (Postgres).
4. **Responde** por WhatsApp (texto, menús/listas, botones, gráficos y **PDF de remisión**).

Es un sistema **multi-usuario / multi-bodega**: cada usuario opera sobre su
bodega asignada, y el estado de cada conversación vive en la base de datos.

### 1.2 Paradigma arquitectónico principal

**Monolito en una sola aplicación FastAPI** que combina tres capas lógicas
claramente separadas por módulos (Service-Layer / DDD-lite), no por procesos:

- **Capa de Presentación / Integración (Webhook)** → `main.py`
  Adaptadores de entrada/salida: Webhook de Meta (Cloud API) y salidas de
  WhatsApp (texto, botones, media/PDF).
- **Capa de Dominio / Servicio (Reglas de negocio)** → `services/inventario_service.py`
  DTOs, enums de estado, validaciones de conservación de masa y stock,
  persistencia atómica vía RPC.
- **Capa de Infraestructura / Adaptadores de salida** → Supabase (DB + Storage),
  DeepSeek (NLU), `generador_pdf.py` (ReportLab), `reporte_grafico.py` (Matplotlib).

Dos patrones de comportamiento destacados:

1. **Máquina de estados conversacional** (state machine per-user): el campo
   `usuarios.contexto_operacion` (JSONB) guarda `borrador_pendiente`,
   `accion_pendiente` (tipo) y `campo_esperado`, guiando cada turno
   (wizards de entrada, venta, anular/corregir remisión, datos de cliente/conductor).
2. **Doble parseo:** un LLM interpreta lenguaje natural, pero la *decisión
   final* la toman reglas deterministas en el `servicio` (validaciones de negocio).

---

## 2. Stack Tecnológico

| Capa | Tecnología | Versión (instalada) | Notas |
|------|-----------|---------------------|-------|
| Lenguaje | **Python** | 3.13 / 3.14 | venv dentro del repo raíz (`Lib/`, `Scripts/`) |
| Web framework | **FastAPI** | - | `main.py` define la `app` + `lifespan` |
| ASGI server | **Uvicorn** | - | Entry: `uvicorn.run("main:app", port=PORT)` |
| DB / Backend | **Supabase** (Postgres) | - | Cliente `supabase`, RPC SQL, Storage |
| HTTP async | **httpx** | - | Cliente saliente: DeepSeek + Graph API |
| NLU (agente) | **DeepSeek API** | - | `deepseek-chat`, `temperature=0`, salida JSON |
| Validación | **Pydantic** | - | Modelo `RespuestaAgente` + `field_validator` |
| ENV | **python-dotenv** | - | `load_dotenv()` al arranque |
| Reportes | **pandas + matplotlib** | - | Backend `Agg` (no interactivo), gráfico de stock |
| PDF | **ReportLab** | - | Remisión replicando modelo `REM_MODELO` |
| Servicio 3rd | **Meta WhatsApp Cloud API** | - | Mensajes entrantes (webhook) y salientes (`/messages`) |
| Legacy (NO usado) | **Twilio** | - | Solo presente en `configurar_webhook.py` (ver §5.2) |

> **Importante (versiones):** `requirements.txt` **no pinnea versiones** (todas
> libres → se instala lo más nuevo). Esto es un riesgo de reproducibilidad. Las
> versiones reales presentes en el venv son las que se listan arriba ("-" = no
> pinneada). Instalación: `pip install -r requirements.txt`.

---

## 3. Estructura de Directorios y Árbol del Proyecto

```
inventario/                                # RAÍZ del proyecto (también es el venv local)
│
├── main.py                                # FastAPI: webhooks + agente IA + enrutamiento
│                                          #   + máquina de estados + prompts + mensajería WhatsApp
├── requirements.txt                       # Dependencies (sin pinning)
├── .env                                    # Secretos (IGNOrado por git, ver .gitignore)
├── .gitignore                              # Excluye Lib/, Scripts/, .env, __pycache__, *.pdf
├── railwayignore..txt                      # Lista de exclusión para deploy Railway
│
├── services/                              # CAPA DE DOMINIO / SERVICIO
│   ├── __init__.py
│   └── inventario_service.py              # Toda la lógica de negocio de inventario:
│                                          #   enums, normalización, validaciones, RPC, remisiones
│
├── generador_pdf.py                       # Salida PDF de remisión (ReportLab, modelo REM_MODELO)
├── reporte_grafico.py                     # Salida imagen: dashboard de stock (tabla + torta) → Storage
│
├── supabase/                              # ⚠️ Migraciones SQL de base de datos
│   ├── migracion_conductores.sql          #   tabla conductores + espejo de clientes + remisiones.conductor_id
│   └── migracion_transformaciones.sql     #   4 estados + catálogo + reutiliza la RPC de transformación
│
├── assets/
│   └── logo_ferroma.jpeg                  # Logo para el encabezado del PDF
│
├── tests/
│   └── test_transformaciones.py           # Pruebas offline (cliente Supabase simulado)
│
├── Scripts de test / utilidades (raíz):
│   ├── test_conexion.py                   # Smokes de deploy/entorno
│   ├── test_vistas.py
│   ├── test_webhook.py / test_webhook_manual.py
│   └── configurar_webhook.py              # ⚠️ Legacy Twilio (credenciales en código)
│
│    ── VIRTUALENV (dentro del repo, ignorado por git) ──
├── Lib/                                   # site-packages del venv (pip)
└── Scripts/                               # ejecutables del venv (pip3.14.exe, etc.)
```

### Responsabilidad de las piezas clave

- **`main.py` (orquestador/gateway):** vive la *lógica conversacional* (wizards,
  menús, botones), el prompt del agente, la fusión de borrador, la validación de
  completitud, la llamada a DeepSeek, los endpoints `/webhook`, la verificación
  de firma HMAC y todo el envío saliente a WhatsApp.
- **`services/inventario_service.py` (reglas de negocio):** *no conoce* de
  WhatsApp ni de DeepSeek; recibe estructuras ya validadas y persiste.
- **`generador_pdf.py` / `reporte_grafico.py`:** adaptador de salida;
  no escriben a DB (excepto el gráfico, que sube a Supabase Storage).

---

## 4. Flujo de Datos y Modelo de Dominio

### 4.1 Flujo de un mensaje (entrada → salida)

```
WhatsApp ──POST──► GET/POST /webhook
                    │  verificación: hmac(META_APP_SECRET, body) == X-Hub-Signature-256
                    │  (sin META_APP_SECRET → 403, NO se procesa nada)
                    ▼
        background_tasks.add_task(procesar_webhook, payload)
                    │
                    ▼
        dedupe: _mensajes_whatsapp_procesados (set, cap 5000)
                    │
                    ▼
        procesar_un_mensaje(usuario, texto)
                    │
        ┌───────────┴─────────────┐
        │ accion_pendiente.tipo?  │  sí → flujo manual (anular/corregir, movimientos…)
        │ campo_esperado?         │  sí → se completa el borrador con la respuesta
        │ (ninguno)               │  no → ir a IA
        └───────────┬─────────────┘
                    ▼
        llamar_deepseek(prompt + borrador) ─► RespuestaAgente (Pydantic)
                    ▼
        fusionar_borrador(anterior, nuevo)      # acumula items/entradas
        _reclasificar_merma_erronea(...)        # safety net de la IA
        validar_completitud(...)                # ¿faltan campos? → pregunta y espera
                    ▼  intención
        dispatch → services.inventario_service (vía asyncio.to_thread)
                    ▼
        _guardar_lote() ─► RPC registrar_lote_inventario (Supabase, atómico)
                    ▼
        respuesta de texto / botones / PDF / gráfico ─► WhatsApp Cloud API
```

### 4.2 Entidades (modelo de dominio → tablas Supabase)

| Tabla / entidad | Rol |
|---|---|
| `usuarios` | actor del chat; `telefono_whatsapp` (login), `bodega_asignada_id`, `contexto_operacion` (JSONB: estado del wizard) |
| `bodegas` | tenancy físico por bodega |
| `materiales` | catálogo con `tipo_material` (**BRUTO/SEMILIMPIO/LIMPIO/MERMA**) y `es_comercializable` |
| `fuentes_origen` | `tipo_fuente` — de dónde entra el material (cooperativa, pesca, planta, compra, proceso…) |
| `movimientos_inventario` | **ledger de doble signo**: `cantidad_kg` +/− , `lote_operacion_id`, `anulado`, `material_id`, `fuente_id`, `bodega_id`, `usuario_id`, `fecha_operacion` |
| `mermas_proceso` | merma histórica que NO genera stock (heredado). Hoy la merma va a `movimientos_inventario` de `MERMA` |
| `clientes` | `nombre`, `identificacion`, `telefono`, `direccion` |
| `conductores` | `nombre`, `identificacion`, `telefono`, `placa` (espejo de clientes) |
| `remisiones` | `numero` (REM_xxx), `lote_operacion_id`, `cliente_id`, `conductor_id`, `bodega_id`, `estado` (ACTIVA/ANULADA) |
| `vista_balance_inventario` | vista SQL que el dashboard lee para el stock por bodega |
| **RPC** `registrar_lote_inventario` | inserta un **lote atómico** (movimientos + mermas) |

Las transacciones de inventario se agrupan en un **`lote_id`
(`lote_operacion_id`)** que vincula todos los movimientos derivados de una misma
operación y permite la anulación y la regeneración del PDF.

**Convención de signos (CRÍTICO):** `cantidad_kg` > 0 es **entrada/stock**;
`cantidad_kg` < 0 es **salida/venta/consumo** (origen en transformaciones,
ventas). Anular una línea devuelve el stock.

### 4.3 Variables de entorno críticas (`.env` / servidor)

| Variable | Requerida | Uso |
|---|---|---|
| `SUPABASE_URL` | ✅ | Cliente Supabase (PostgREST) |
| `SUPABASE_KEY` | ✅ | API key Supabase (service role típicamente) |
| `DEEPSEEK_API_KEY` | ✅ | Autenticación de la API de DeepSeek |
| `VERIFY_TOKEN` | ✅ | Handshake del webhook de Meta (GET /webhook) |
| `WHATSAPP_TOKEN` | ✅ | Token Bearer del Graph API para enviar mensajes |
| `PHONE_NUMBER_ID` | ✅ | ID del número de WhatsApp (destino `messages`) |
| `META_APP_SECRET` | ✅ | Firmar/verificar HMAC del webhook (X-Hub-Signature-256) |
| `PORT` | 默认 10000 | Puerto de Uvicorn |

**Zona horaria:** fija `ZoneInfo("America/Bogota")` (BOGOTA) — **hardcodeada**
en `main.py`; la fecha de operación se deriva del `timestamp` del mensaje de Meta.

---

## 5. Puntos de Extensión y Reglas de Desarrollo

### 5.1 Patrones que DEBES respetar (romper esto = romper el diseño)

1. **Escritura atómica única → `_guardar_lote()` (RPC).**
   Todo método de negocio de escritura (entrada revuelto, compra, transformación,
   selección, ajuste, venta…) termina llamando a `_guardar_lote(movimientos,
   mermas)`, que invoca la RPC `registrar_lote_inventario`. **NUNCA insertar
   `movimientos_inventario` directamente** con `.table(...).insert()` sueltos:
   rompería la integridad del lote y la anulación.
2. **El servicio `inventario_service.py` ignora a WhatsApp.** No importes `main`
   desde `services`; toda la orquestación conversacional vive en `main.py`.
3. **Mantén el loop de eventos no bloqueado:** toda llamada *síncrona* del
   servicio de inventario en `main.py` se ejecuta con `asyncio.to_thread(...)`.
   Añade las nuevas llamadas de igual forma.
4. **Deduplicación de mensajes:** todo `message["id"]` pasa por
   `_mensajes_whatsapp_procesados` (cap `_MAX_MENSAJES_PROCESADOS`=5000). Meta
   reintenta webhooks; sin esto, el bot responderá/enviará duplicado.
5. **Validación de firma HMAC obligatoria:** si `META_APP_SECRET` está vacío,
   el POST /webhook devuelve 403 y NO procesa nada (seguridad).
6. **Conservación de masa en transformación:**
   `cantidad_procesada == Σ salidas + merma`. Valida stock suficiente y que
   ningún producto sea BRUTO. No la sedimentes.
7. **Catálogo de 4 estados + MERMA vendible:** `es_comercializable=true` en
   Basura/Tierra. No asumas que la merma no tiene stock.
8. **Normalización y sinónimos de negocio:** usa `normalizar()` (sin tildes,
   minúsculas) antes de comparar nombres; aplica sinónimos (`grueso→carter`,
   `rechazo→arreglo`) y frases (`arreglo cobre→arreglo cobre y bronce`)
   **antes** de buscar en el catálogo.
9. **Errores de negocio en español con `ValueError`:** el mensaje de la
   excepción llega directo al usuario por WhatsApp (la capa no traduce).
10. **Corrección de remisión conserva el MISMO número:** tras corregir nunca
    llamar a `generar_numero_remision()`; usa `obtener_datos_pdf_remision()` +
    `generar_remision_pdf_archivo(...)`.

### 5.2 Áreas críticas / sensibles al refactorizar

- **⚠️ Shadowing `supabase/` vs paquete `supabase`.** El directorio local
  `supabase/` (migraciones SQL) **ensombrece** al paquete pip `supabase` al
  importar desde la raíz (`from supabase import Client` falla: "unknown
  location"). Afecta a `main.py` en runtime. **Acción recomendada: renombrar
  `supabase/` a `sql/`** o `migraciones/`. Los tests hoy inyectan un stub:
  ```py
  _stub = types.ModuleType("supabase"); _stub.Client = object
  sys.modules.setdefault("supabase", _stub)
  ```
- **⚠️ Esquema incompleto en el repo.** La RPC `registrar_lote_inventario`,
  las tablas base (`movimientos_inventario`, `materiales`, `fuentes_origen`,
  `usuarios`, `bodegas`…) y la vista `vista_balance_inventario` **NO están
  versionadas**; solo hay `migracion_conductores.sql` y
  `migracion_transformaciones.sql`. Antes de desplegar a un entorno nuevo hay
  que regenerar todo el esquema (o documentarlo).
- **Credenciales en código (secret):** `configurar_webhook.py` trae
  `account_sid`/`auth_token` de **Twilio** en texto plano y apunta a una URL de
  ngrok. Es Legacy: la integración activa es **Meta Cloud API** (no Twilio).
  Revisar/eliminar antes de producción para no filtrar secretos.
- **`reporte_grafico.py` hace Side Effects al import:** crea un cliente Supabase
  a nivel de módulo con `load_dotenv()` → difícil de importar sin env. Mantén
  `matplotlib.use('Agg')` antes de `import pyplot` (crítica en servidores headless).
- **No ejecutar sin credenciales:** sin `SUPABASE_URL/KEY`, `supabase=None` /
  `inventario=None` y fallará cualquier mensaje entrante. Protege esos paths.

### 5.3 Cómo validar sin tocar el ambiente real

```powershell
# Compilación
python -m py_compile main.py services\inventario_service.py

# Pruebas offline (no requiere DB real; 23 casos)
python -m tests.test_transformaciones
```

Los tests simulan el cliente Supabase (`FakeSupabase`, `_Query`, `_RpcBuilder`)
y cubren: reglas de transformación 1/2/3, conservación de masa, stock
insuficiente, venta de MERMA, y `test_regenerar_pdf_datos`.

---

## 6. Referencia rápida: intenciones y máquinas de estado

Intenciones de `RespuestaAgente.intencion`:

```
REGISTRO_DIARIO | ENTRADA_REVUELTO | SELECCION_REVUELTO |
TRANSFORMACION_MATERIAL | COMPRA_DIRECTA | VENTA_DESPACHO |
AJUSTE_INVENTARIO | CONSULTA | OTRO
```

`main.py` mapea cada intención a un método del servicio:
`registrar_registro_diario / registrar_entrada_revuelto /
registrar_seleccion_revuelto / registrar_transformacion_material /
registrar_compra_directa / registrar_venta_multiple /
registrar_ajuste_inventario / obtener_saldo(s) / obtener_reporte_diario_texto`.

**Anular/corregir remisión** es una máquina de estado **manual** (no pasa por la
IA): `espera_remision_modo → espera_numero_remision → espera_alcance →
corregir_opciones → espera_material | correccion_rem_cliente → finalizar →
regenerar_y_enviar_pdf_remision(...)`.

**Venta/despacho** usa un wizard por pasos para cliente y conductor (pide un
campo a la vez; si el cliente/conductor ya existe, solo rellena los faltantes).

---

## 7. Diagrama de dependencias

```
main.py ──► services/inventario_service.py   (lógica de negocio + Supabase)
   │  └──── asyncio.to_thread(...)
   ├──► generador_pdf.py       (PDF → archivo temporal → media de WhatsApp)
   └──► reporte_grafico.py     (PNG → Supabase Storage → URL pública)
                                     ▲
DeepSeek API ◄── main.py (httpx)      │
Meta Cloud API ◄──► main.py (webhook / firmas / envío)
```

Dirección de dependencias: **main.py depende de todo; nada depende de main.py**.
Mantener esa flecha hacia abajo para no crear ciclos de import.