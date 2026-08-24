import asyncio
import io
import hashlib
import hmac
import json
import logging
import os
import uvicorn
import tempfile
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Literal, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response
from pydantic import BaseModel, Field, field_validator
from supabase import Client, create_client

from reporte_grafico import generar_y_subir_grafico_stock
from generador_pdf import generar_remision_pdf_archivo
from services.inventario_service import InventarioServiceConValidacion

# Configuración de Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cargar variables de entorno
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()

logger.warning("=" * 60)
logger.warning("VERIFICACIÓN DE VARIABLES DE CONFIGURACIÓN:")
logger.warning(f"✓ SUPABASE_URL: {'✅' if SUPABASE_URL else '❌'}")
logger.warning(f"✓ SUPABASE_KEY: {'✅' if SUPABASE_KEY else '❌'}")
logger.warning(f"✓ DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
logger.warning(f"✓ VERIFY_TOKEN: {'✅' if VERIFY_TOKEN else '❌'}")
logger.warning(f"✓ WHATSAPP_TOKEN: {'✅' if WHATSAPP_TOKEN else '❌'}")
logger.warning(f"✓ PHONE_NUMBER_ID: {'✅' if PHONE_NUMBER_ID else '❌'}")
logger.warning(f"✓ META_APP_SECRET: {'✅' if META_APP_SECRET else '❌'}")
logger.warning("=" * 60)

# Inicialización de Supabase
supabase: Optional[Client] = None
inventario: Optional[InventarioServiceConValidacion] = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        inventario = InventarioServiceConValidacion(supabase)
        logger.info("✅ Supabase conectado")
    except Exception as e:
        logger.error(f"❌ Error conectando a Supabase: {e}")
else:
    logger.warning("⚠️ Supabase no configurado")

http_client: Optional[httpx.AsyncClient] = None
BOGOTA = ZoneInfo("America/Bogota")

# =====================================================================
# PUNTO 2 (B): CACHÉ DE DEDUPLICACIÓN EN MEMORIA (REINTENTOS META)
# =====================================================================
PROCESSED_MESSAGES_CACHE: Dict[str, datetime] = {}
CACHE_TTL_MINUTES = 10

def es_mensaje_duplicado(message_id: str) -> bool:
    """Evita ejecuciones dobles limpiando dinámicamente los IDs antiguos."""
    ahora = datetime.now()
    limite = ahora - timedelta(minutes=CACHE_TTL_MINUTES)
    
    # Pruning reactivo
    remociones = [m_id for m_id, ts in PROCESSED_MESSAGES_CACHE.items() if ts < limite]
    for m_id in remociones:
        PROCESSED_MESSAGES_CACHE.pop(m_id, None)
        
    if message_id in PROCESSED_MESSAGES_CACHE:
        logger.warning(f"♻️ Reintento duplicado detectado de Meta API. Ignorando Message ID: {message_id}")
        return True
        
    PROCESSED_MESSAGES_CACHE[message_id] = ahora
    return False

# Expresiones regulares y utilidades
LINEA_MATERIAL_CANTIDAD = re.compile(r"^\s*(.+?)[\s\-:]+(\d+(?:[.,]\d+)?)\s*(?:kg)?\s*$", re.IGNORECASE)

def parsear_material_cantidad(texto: str) -> Optional[tuple]:
    m = LINEA_MATERIAL_CANTIDAD.match(texto.strip())
    if not m:
        return None
    return m.group(1).strip(), float(m.group(2).replace(",", "."))

CAMPOS_CLIENTE = {
    "nombre": "nombre", 
    "documento": "identificacion", 
    "identificacion": "identificacion",
    "telefono": "telefono", 
    "celular": "telefono", 
    "direccion": "direccion"
}

def parsear_campos_cliente(texto: str) -> Dict[str, str]:
    campos = {}
    for parte in re.split(r"[,\n;]+", texto):
        m = re.match(r"\s*(\w+)\s*[:\-]?\s*(.+)", parte.strip())
        if not m:
            continue
        clave = m.group(1).strip().lower()
        valor = m.group(2).strip()
        if clave in CAMPOS_CLIENTE and valor:
            campos[CAMPOS_CLIENTE[clave]] = valor
    return campos


def parsear_campos_cliente_venta(texto: str) -> Dict[str, str]:
    mapeo = {
        "nombre": "cliente",
        "documento": "cliente_documento",
        "identificacion": "cliente_documento",
        "direccion": "cliente_direccion",
        "telefono": "cliente_celular",
        "celular": "cliente_celular",
        "placa": "cliente_placa",
        "vehiculo": "cliente_placa",
        "conductor": "cliente_conductor",
        "chofer": "cliente_conductor",
        "id": "cliente_conductor_id",
        "cedula": "cliente_conductor_id",
        "celular_conductor": "cliente_conductor_celular",
        "tel_conductor": "cliente_conductor_celular"
    }
    campos = {}
    partes = [p.strip() for p in re.split(r"[,\n;]+", texto) if p.strip()]
    
    for parte in partes:
        m = re.match(r"\s*(\w+)\s*[:\-]?\s*(.+)", parte)
        if m and m.group(1).strip().lower() in mapeo and m.group(2).strip():
            campos[mapeo[m.group(1).strip().lower()]] = m.group(2).strip()
            
    # Orden estricto por comas: [Nombre Conductor, ID Conductor, Placa, Celular Conductor]
    if not campos and partes:
        if len(partes) >= 4:
            campos["cliente_conductor"] = partes[0]
            campos["cliente_conductor_id"] = partes[1]
            campos["cliente_placa"] = partes[2]
            campos["cliente_conductor_celular"] = partes[3]
        elif len(partes) == 3:
            campos["cliente_conductor"] = partes[0]
            campos["cliente_conductor_id"] = partes[1]
            campos["cliente_placa"] = partes[2]
        elif len(partes) == 2:
            campos["cliente_conductor"] = partes[0]
            campos["cliente_placa"] = partes[1]
        elif len(partes) == 1:
            campos["cliente_conductor"] = partes[0]
            
    return campos

FECHA_COLOMBIANA = re.compile(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?$")

def parsear_fecha_colombiana(texto: str) -> Optional[str]:
    m = FECHA_COLOMBIANA.match(texto.strip())
    if not m:
        return None
    dia, mes, anio = m.groups()
    dia, mes = int(dia), int(mes)
    if anio is None:
        anio = datetime.now(BOGOTA).year
    else:
        anio = int(anio)
        if anio < 100:
            anio += 2000
    try:
        return date(anio, mes, dia).isoformat()
    except ValueError:
        return None


def formatear_movimientos_material(resultado: Dict[str, Any]) -> str:
    lineas = [f"📑 Movimientos de {resultado['material']}", ""]
    saldo_inicial = resultado.get("saldo_inicial")
    if saldo_inicial:
        lineas.append(f"Saldo inicial (antes del rango): {saldo_inicial:,.2f} kg")
        lineas.append("")
    movimientos = resultado.get("movimientos", [])
    if not movimientos:
        lineas.append("No hay movimientos en ese rango.")
    else:
        for mv in movimientos:
            signo = "+" if mv["cantidad_kg"] >= 0 else ""
            fuente = f" ({mv['fuente']})" if mv.get("fuente") else ""
            lineas.append(
                f"• {mv['fecha']} — {mv['tipo']}: {signo}{mv['cantidad_kg']:,.2f} kg{fuente} "
                f"| saldo: {mv['saldo_acumulado']:,.2f} kg"
            )
        return "\n".join(lineas)

def clean_payload(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: clean_payload(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, list):
        return [clean_payload(v) for v in obj if v is not None]
    return obj


class ItemMaterial(BaseModel):
    material_nombre: str
    cantidad_kg: float
    precio_unitario: Optional[float] = 0.0


class EntradaRevuelto(BaseModel):
    fuente_nombre: str
    cantidad_kg: float


class RespuestaAgente(BaseModel):
    intencion: Literal["REGISTRO_DIARIO", "ENTRADA_REVUELTO", "SELECCION_REVUELTO", "COMPRA_DIRECTA", "VENTA_DESPACHO", "AJUSTE_INVENTARIO", "CONSULTA", "OTRO"] = "OTRO"
    fecha_operacion: Optional[str] = None
    entradas_revuelto: List[EntradaRevuelto] = Field(default_factory=list)
    items: List[ItemMaterial] = Field(default_factory=list)
    cantidad_revuelto_procesada: Optional[float] = None
    merma_kg: float = 0.0
    fuente_compra: Optional[str] = None
    cliente: Optional[str] = None
    cliente_documento: Optional[str] = None
    cliente_direccion: Optional[str] = None
    cliente_placa: Optional[str] = None
    cliente_conductor: Optional[str] = None
    cliente_conductor_id: Optional[str] = None
    cliente_celular: Optional[str] = None
    cliente_conductor_celular: Optional[str] = None
    consulta_material: Optional[str] = None
    respuesta_texto: str = ""

    @field_validator("fecha_operacion")
    @classmethod
    def fecha_iso(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        datetime.strptime(value, "%Y-%m-%d")
        return value


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(35.0))
    yield
    await http_client.aclose()


app = FastAPI(title="Agente de Inventario", lifespan=lifespan)


def fecha_local_mensaje(message: Dict[str, Any]) -> str:
    marca = message.get("timestamp")
    if marca:
        return datetime.fromtimestamp(int(marca), tz=BOGOTA).date().isoformat()
    return datetime.now(BOGOTA).date().isoformat()


async def enviar_mensaje_whatsapp_json(payload: dict) -> None:
    if not http_client:
        logger.error("❌ http_client no inicializado")
        return

    phone_id = str(PHONE_NUMBER_ID).strip().replace('"', '').replace("'", "")
    token_limpio = str(WHATSAPP_TOKEN).strip().replace('"', '').replace("'", "")
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {token_limpio}",
        "Content-Type": "application/json"
    }
    
    cleaned = clean_payload(payload)
    destino = cleaned.get("to", "Desconocido")
    
    try:
        response = await http_client.post(url, json=cleaned, headers=headers)
        if response.status_code == 401:
            logger.error("❌ Error 401: Token inválido.")
            return
        response.raise_for_status()
        logger.info(f"✅ Respuesta WhatsApp API: {response.status_code}")
    except httpx.ReadError as exc:
        logger.error(f"📡 Error de red temporal en Render controlado: {exc}")
    except Exception as e:
        logger.error(f"⚠️ Error controlado en el envío de WhatsApp: {e}")


async def enviar_mensaje_whatsapp(destino: str, texto: str) -> None:
    to_clean = re.sub(r"\D", "", str(destino))
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_clean,
        "type": "text",
        "text": {"body": str(texto)[:4096]}
    }
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_botones_whatsapp(destino: str, texto: str, opciones: List[tuple]) -> None:
    to_clean = re.sub(r"\D", "", str(destino))
    botones = [{"type": "reply", "reply": {"id": id_, "title": titulo[:20]}} for id_, titulo in opciones[:3]]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_clean,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": botones},
        },
    }
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_imagen_whatsapp(destino: str, url_imagen: str, leyenda: str) -> None:
    to_clean = re.sub(r"\D", "", str(destino))
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_clean,
        "type": "image",
        "image": {"link": url_imagen, "caption": leyenda}
    }
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_documento_whatsapp(destino: str, ruta_archivo: str, nombre_documento: str = "documento.pdf") -> None:
    """Función Máxima: Resuelve de raíz el fallo de archivos HTML corruptos en Supabase."""
    if not http_client:
        logger.error("❌ http_client no inicializado")
        return

    try:
        await asyncio.sleep(2.5) # Garantizar cierre de descriptor de archivo en Render
        if not os.path.exists(ruta_archivo) or os.path.getsize(ruta_archivo) == 0:
            logger.error("❌ Archivo vacío o inexistente")
            return

        url_documento = None
        bucket_name = "documentos" 
        nombre_remoto = f"remisiones/{int(datetime.now(BOGOTA).timestamp())}_{nombre_documento}"

        if supabase:
            try:
                with open(ruta_archivo, "rb") as f:
                    binario = f.read()

                archivo_virtual = io.BytesIO(binario)
                def _subir():
                    return supabase.storage.from_(bucket_name).upload(
                        path=nombre_remoto, file=archivo_virtual,
                        file_options={"content-type": "application/pdf", "upsert": "true"}
                    )

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _subir)
                
                res_url = supabase.storage.from_(bucket_name).get_public_url(nombre_remoto)
                url_documento = res_url if isinstance(res_url, str) else getattr(res_url, "public_url", str(res_url))
            except Exception as e:
                logger.error(f"❌ Error en subida: {e}")

        if not url_documento:
            base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip('/') or "https://inventario-qcza.onrender.com"
            url_documento = f"{base_url}/download/{os.path.basename(ruta_archivo)}"

        to_clean = re.sub(r"\D", "", str(destino))
        payload = {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": to_clean,
            "type": "document", "document": {"link": url_documento, "filename": nombre_documento}
        }
        await enviar_mensaje_whatsapp_json(payload)
    except Exception as e:
        logger.error(f"❌ Error crítico en documento: {e}")


async def subir_archivo_supabase(ruta_archivo: str, nombre_documento: str) -> str:
    with open(ruta_archivo, 'rb') as f:
        contenido = f.read()
    ruta_storage = f"remisiones/{int(datetime.now(BOGOTA).timestamp())}_{nombre_documento}"
    supabase.storage.from_("documentos").upload(ruta_storage, contenido, {"content-type": "application/pdf"})
    return supabase.storage.from_("documentos").get_public_url(ruta_storage)


def prompt_agente(*, usuario: str, bodega_id: int, fecha_mensaje: str, borrador: Dict[str, Any]) -> str:
    materiales = [{"nombre": x.nombre, "tipo": x.tipo_material, "comercializable": x.es_comercializable} for x in inventario.catalogo_materiales.values()]
    fuentes = [x.nombre for x in inventario.catalogo_fuentes.values()]
    return f'''Eres el extractor de datos de un inventario de reciclaje. Devuelve SOLO un objeto JSON válido, sin Markdown.
Usuario: {usuario}; bodega: {bodega_id}; fecha local real del mensaje: {fecha_mensaje}.
Materiales permitidos: {json.dumps(materiales, ensure_ascii=False)}.
Fuentes permitidas: {json.dumps(fuentes, ensure_ascii=False)}.
Borrador de conversación previo: {json.dumps(borrador, ensure_ascii=False)}.
... (Reglas de negocio idénticas del prompt base de producción) ...'''


async def llamar_deepseek(prompt: str, mensaje: str) -> RespuestaAgente:
    assert http_client is not None
    respuesta = await http_client.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "temperature": 0, "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": mensaje}]},
    )
    respuesta.raise_for_status()
    contenido = respuesta.json()["choices"][0]["message"]["content"]
    contenido = re.sub(r"^```(?:json)?\s*|\s*```$", "", contenido.strip(), flags=re.I)
    return RespuestaAgente.model_validate_json(contenido)


def fusionar_borrador(anterior: Dict[str, Any], nuevo: RespuestaAgente) -> Dict[str, Any]:
    datos = nuevo.model_dump(exclude_none=True)
    resultado = dict(anterior)
    
    if "items" in datos:
        items_existentes = {i["material_nombre"].lower(): i for i in resultado.get("items", [])}
        for item in datos["items"]:
            mat_key = item["material_nombre"].lower()
            if mat_key in items_existentes:
                items_existentes[mat_key]["cantidad_kg"] += item["cantidad_kg"]
                if item.get("precio_unitario"):
                    items_existentes[mat_key]["precio_unitario"] = item["precio_unitario"]
            else:
                items_existentes[mat_key] = item
        resultado["items"] = list(items_existentes.values())
        datos.pop("items")

    if "entradas_revuelto" in datos:
        existentes_rev = resultado.get("entradas_revuelto", [])
        existentes_rev.extend(datos["entradas_revuelto"])
        resultado["entradas_revuelto"] = existentes_rev
        datos.pop("entradas_revuelto")

    for clave, valor in datos.items():
        if valor in ([], ""):
            continue
        if clave == "intencion" and valor == "OTRO" and resultado.get("intencion") not in (None, "OTRO"):
            continue
        resultado[clave] = valor
    return resultado


def validar_completitud(datos: Dict[str, Any], fecha_mensaje: str, cliente_existe: bool = False) -> Optional[Tuple[str, str]]:
    intento = datos.get("intencion", "OTRO")
    
    for k in ["items", "entradas_revuelto"]: datos.setdefault(k, [])
    for k in ["cliente", "cliente_documento", "cliente_direccion", "cliente_placa", "cliente_conductor", "cliente_celular", "cliente_conductor_id", "cliente_conductor_celular"]: datos.setdefault(k, None)

    if intento in ("ENTRADA_REVUELTO", "REGISTRO_DIARIO") and not datos.get("entradas_revuelto"):
        return "Indica las fuentes y los kilos de Revuelto, por ejemplo: Cooperativa 500.", "entradas_revuelto"
    if intento in ("REGISTRO_DIARIO", "COMPRA_DIRECTA", "VENTA_DESPACHO", "AJUSTE_INVENTARIO") and not datos.get("items"):
        return "Indica los materiales y kilos que se deben registrar.", "items"
    if intento == "SELECCION_REVUELTO" and not datos.get("items") and not datos.get("merma_kg", 0):
        return "Indica los materiales seleccionados o la cantidad de basura a descontar del Revuelto.", "items"
    
    if intento == "VENTA_DESPACHO":
        if not datos.get("cliente"): return "Indica el nombre del cliente, por favor.", "cliente"
        if not cliente_existe:
            faltantes = [f for f, c in [("documento", "cliente_documento"), ("dirección", "cliente_direccion"), ("celular", "cliente_celular")] if not datos.get(c)]
            if faltantes: return f"Es un cliente nuevo, para registrarlo también necesito su {', '.join(faltantes)}.", "cliente_datos"
        
        faltantes_c = [f for f, c in [("nombre del conductor", "cliente_conductor"), ("ID / cédula", "cliente_conductor_id"), ("placa del vehículo", "cliente_placa"), ("celular del conductor", "cliente_conductor_celular")] if not datos.get(c)]
        if faltantes_c:
            return f"Falta indicar: {', '.join(faltantes_c)}.\n\nPor favor, envía los datos completos del conductor y vehículo (nombre, ID, placa y celular).", "conductor_datos"

    if intento == "COMPRA_DIRECTA" and not datos.get("fuente_compra"): datos["fuente_compra"] = "Compras"
    if intento in ("REGISTRO_DIARIO", "ENTRADA_REVUELTO", "SELECCION_REVUELTO", "COMPRA_DIRECTA", "VENTA_DESPACHO", "AJUSTE_INVENTARIO") and not datos.get("fecha_operacion"):
        return "¿Qué fecha fue esta operation? Responde 'hoy', 'ayer' o dd-mm-aaaa.", "fecha_operacion"
    return None


async def guardar_contexto(usuario_id: int, contexto: Dict[str, Any]) -> None:
    await asyncio.to_thread(lambda: supabase.table("usuarios").update({"contexto_operacion": contexto}).eq("id", usuario_id).execute())


async def procesar_un_mensaje(message: Dict[str, Any], contactos: List[Dict[str, Any]]) -> None:
    tipo_mensaje = message.get("type")
    if tipo_mensaje == "text": texto = message.get("text", {}).get("body", "").strip()
    elif tipo_mensaje == "interactive":
        interactivo = message.get("interactive", {})
        texto = interactivo.get("button_reply", {}).get("id") or interactivo.get("list_reply", {}).get("id") or ""
    else: return

    telefono = str(message.get("from", "")).replace("+", "")
    if not phone_id or not texto: telefono = str(message.get("from", "")).replace("+", "")
    if not telefono or not texto: return

    usuarios = await asyncio.to_thread(lambda: supabase.table("usuarios").select("*,bodegas(nombre)").eq("telefono_whatsapp", telefono).execute())
    if not usuarios.data:
        await enviar_mensaje_whatsapp(telefono, "Acceso denegado: número no registrado.")
        return
    usuario = usuarios.data[0]
    usuario_id, bodega_id = usuario["id"], usuario.get("bodega_asignada_id")
    if not bodega_id: return

    contexto = usuario.get("contexto_operacion") or {}
    if texto.lower() in {"cancelar", "limpiar", "reiniciar"}:
        contexto.update({"borrador_pendiente": {}, "accion_pendiente": {}, "campo_esperado": None})
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "Operación cancelada.")
        return

    accion = contexto.get("accion_pendiente") or {}
    if accion.get("tipo"):
        respuesta_texto = None
        try:
            if accion["tipo"] == "espera_numero_remision":
                remision = await asyncio.to_thread(inventario.obtener_remision, texto)
                if not remision: respuesta_texto, contexto["accion_pendiente"] = f"No encontré la remisión '{texto}'.", {}
                else: contexto["accion_pendiente"], respuesta_texto = {"tipo": "espera_alcance", "numero": remision["numero"]}, f"¿Deseas anular TODA la remisión {remision['numero']}? (sí/no)"
            elif accion["tipo"] == "espera_alcance":
                if texto.strip().lower() in {"si", "sí"}:
                    r = await asyncio.to_thread(inventario.anular_remision_completa, accion["numero"], usuario_id)
                    respuesta_texto, contexto["accion_pendiente"] = f"Remisión {r['numero']} de baja.", {}
                elif texto.strip().lower() == "no":
                    contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": accion["numero"]}
                    respuesta_texto = "Digite los datos que desea modificar (ejemplo: Carter 3500)."
            elif accion["tipo"] == "espera_material":
                par = parsear_material_cantidad(texto)
                if not par: respuesta_texto = "Escribe así: Material cantidad."
                else:
                    mat_n, cant = par
                    r = await asyncio.to_thread(inventario.anular_o_actualizar_linea, numero=accion["numero"], material_nombre=mat_n, cantidad_kg=cant, usuario_id=usuario_id)
                    if r["accion"] == "anulada": respuesta_texto, contexto["accion_pendiente"] = f"Se anuló {r['material']}.", {}
                    else:
                        contexto["accion_pendiente"] = {"tipo": "espera_confirmacion_actualizacion", "numero": accion["numero"], "movimiento_id": r["movimiento_id"], "material": r["material"], "cantidad_nueva": r["cantidad_nueva"]}
                        respuesta_texto = f"¿Actualizar a {r['cantidad_nueva']:,.2f} kg? (sí/no)"
            elif accion["tipo"] == "espera_confirmacion_actualizacion":
                if texto.strip().lower() in {"si", "sí"}:
                    await asyncio.to_thread(inventario.actualizar_cantidad_linea, movimiento_id=accion["movimiento_id"], nueva_cantidad_kg=accion["cantidad_nueva"])
                    respuesta_texto, contexto["accion_pendiente"] = "Actualizado con éxito.", {}
                else: contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": accion["numero"]}; respuesta_texto = "Modificación descartada."
            
            # =====================================================================
            # PUNTO 1: BÚSQUEDA INTELIGENTE DE MATERIALES (EVITA MATCH ERRÓNEO)
            # =====================================================================
            elif accion["tipo"] == "movimientos_material":
                texto_buscado = texto.lower().strip()
                coincidencia_exacta = None
                coincidencias_parciales = []

                for mat in inventario.catalogo_materiales.values():
                    nom_mat = mat.nombre.lower().strip()
                    if nom_mat == texto_buscado:
                        coincidencia_exacta = mat
                        break
                    elif texto_buscado in nom_mat:
                        coincidencias_parciales.append(mat)

                mat_final = coincidencia_exacta or (coincidencias_parciales[0] if len(coincidencias_parciales) == 1 else None)

                if mat_final:
                    contexto["accion_pendiente"] = {"tipo": "movimientos_rango", "material": mat_final.nombre}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_botones_whatsapp(telefono, f"Rango para {mat_final.nombre}:", [("todo", "Todo"), ("rango", "Elegir fechas")])
                    return
                elif len(coincidencias_parciales) > 1:
                    opciones_botones = [(f"MAT_{m.id}"[:20], m.nombre[:20]) for m in coincidencias_parciales[:3]]
                    await enviar_botones_whatsapp(telefono, "Encontré múltiples opciones. Selecciona cuál buscas:", opciones_botones)
                    return
                else:
                    respuesta_texto = f"No encontré el material '{texto}'."
                    contexto["accion_pendiente"] = {}

            elif accion["tipo"] == "movimientos_rango":
                if texto.strip().lower() in {"todo", "1"}:
                    resultado = await asyncio.to_thread(inventario.obtener_movimientos_material, bodega_id=bodega_id, material_nombre=accion["material"])
                    respuesta_texto, contexto["accion_pendiente"] = formatear_movimientos_material(resultado), {}
                else: contexto["accion_pendiente"], respuesta_texto = {"tipo": "movimientos_desde", "material": accion["material"]}, "Fecha desde (dd-mm-aaaa):"
            elif accion["tipo"] == "movimientos_desde":
                contexto["accion_pendiente"] = {"tipo": "movimientos_hasta", "material": accion["material"], "fecha_desde": parsear_fecha_colombiana(texto)}
                respuesta_texto = "Fecha hasta (dd-mm-aaaa):"
            elif accion["tipo"] == "movimientos_hasta":
                resultado = await asyncio.to_thread(inventario.obtener_movimientos_material, bodega_id=bodega_id, material_nombre=accion["material"], fecha_desde=accion["fecha_desde"], fecha_hasta=parsear_fecha_colombiana(texto))
                respuesta_texto, contexto["accion_pendiente"] = formatear_movimientos_material(resultado), {}
        except Exception as e:
            respuesta_texto, contexto["accion_pendiente"] = f"Fallo operativo: {e}", {}
        
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, respuesta_texto)
        return

    # Comandos Rápidos Directos por Texto
    texto_normalizado = texto.lower().strip()
    if texto_normalizado == "anular":
        contexto["accion_pendiente"] = {"tipo": "espera_numero_remision"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "¿Qué remisión deseas anular? (ej: REM_112)")
        return
    if texto_normalizado in {"ver grafico", "reporte visual"}:
        url = await asyncio.to_thread(generar_y_subir_grafico_stock, bodega_id)
        if url: await enviar_imagen_whatsapp(telefono, url, "Inventario Visual")
        else: await enviar_mensaje_whatsapp(telefono, "Sin datos para gráfico.")
        return

    # PUNTO 3 (A): REMOCIÓN DE DUPLICACIÓN EN ESPEJO DE MOVIMIENTOS
    if texto_normalizado in {"ver_inventario", "ver inventario", "saldos", "inventario", "2"}:
        contexto.update({"borrador_pendiente": {}, "campo_esperado": None})
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(telefono, "¿Qué deseas consultar?", [("inv_total", "Inventario total"), ("inv_movs", "Movimientos"), ("inv_hoy", "Reporte de hoy")])
        return

    if texto_normalizado == "inv_total":
        saldos = await asyncio.to_thread(inventario.obtener_saldos_bodega, bodega_id)
        if not saldos: await enviar_mensaje_whatsapp(telefono, "Sin stock.")
        else:
            lineas = [f"📦 Stock Bodega #{bodega_id}:"] + [f"• {x['material']}: {x['saldo_kg']:,.2f} kg" for x in sorted(saldos, key=lambda x: x["saldo_kg"], reverse=True)]
            await enviar_mensaje_whatsapp(telefono, "\n".join(lineas))
        return

    if texto_normalizado in {"inv_movs", "movimientos"}:
        contexto["accion_pendiente"] = {"tipo": "movimientos_material"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "¿De qué material deseas ver los movimientos?")
        return

    if texto_normalizado in {"inv_hoy", "reporte de hoy"}:
        reporte = await asyncio.to_thread(inventario.obtener_reporte_diario_texto, bodega_id, fecha_local_mensaje(message))
        await enviar_mensaje_whatsapp(telefono, reporte)
        return

    if texto in {"1", "Ingresar Inventario"}:
        contexto["campo_esperado"] = "menu_ingreso"
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(telefono, "Selecciona movimiento:", [("entrada", "Entrada"), ("arreglo", "Arreglo"), ("salida", "Salida")])
        return

    await asyncio.to_thread(inventario.recargar_catalogos)
    fecha_mensaje = fecha_local_mensaje(message)
    borrador_anterior = contexto.get("borrador_pendiente") or {}
    campo_esperado = contexto.get("campo_esperado")

    # PUNTO 3 (B): COMPACTACIÓN ATÓMICA DE CONDUCTOR_DATOS REDUNDANTES
    datos = dict(borrador_anterior)
    if campo_esperado == "cliente": datos["cliente"] = texto.strip()
    elif campo_esperado == "cliente_datos": datos.update(parsear_campos_cliente_venta(texto))
    elif campo_esperado == "conductor_datos":
        campos_ext = parsear_campos_cliente_venta(texto)
        datos.update({k: v for k, v in campos_ext.items() if v})
        if not all(datos.get(k) for k in ["cliente_conductor", "cliente_conductor_id", "cliente_placa", "cliente_conductor_celular"]):
            partes = [p.strip() for p in re.split(r"[,\n;]+", texto) if p.strip()]
            if len(partes) >= 4:
                datos.update({"cliente_conductor": partes[0], "cliente_conductor_id": partes[1], "cliente_placa": partes[2], "cliente_conductor_celular": partes[3]})
    elif campo_esperado in {"tipo_movimiento", "menu_ingreso"}:
        elec = texto.strip().lower()
        if elec in {"1", "entrada"}: datos["intencion"] = "AJUSTE_INVENTARIO"
        elif elec in {"2", "arreglo"}: datos["intencion"] = "SELECCION_REVUELTO"
        elif elec in {"3", "salida", "venta"}: datos["intencion"] = "VENTA_DESPACHO"
    else:
        try:
            ai = await llamar_deepseek(prompt_agente(usuario=usuario["nombre"], bodega_id=bodega_id, fecha_mensaje=fecha_mensaje, borrador=borrador_anterior), texto)
            datos = fusionar_borrador(borrador_anterior, ai)
        except Exception:
            await enviar_mensaje_whatsapp(telefono, "Fallo de interpretación.")
            return

    cliente_existente = None
    if datos.get("intencion") == "VENTA_DESPACHO" and datos.get("cliente"):
        cliente_existente = await asyncio.to_thread(inventario.obtener_cliente_por_nombre, datos["cliente"])
        if cliente_existente:
            datos.update({"cliente_documento": cliente_existente.get("identificacion"), "cliente_direccion": cliente_existente.get("direccion"), "cliente_celular": cliente_existente.get("telefono")})

    if datos.get("intencion") in (None, "OTRO") and datos.get("items"):
        contexto.update({"borrador_pendiente": datos, "campo_esperado": "tipo_movimiento"})
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(telefono, "Selecciona el movimiento:", [("entrada", "Entrada"), ("arreglo", "Arreglo"), ("salida", "Salida")])
        return

    resultado_validacion = validar_completitud(datos, fecha_mensaje, cliente_existe=bool(cliente_existente))
    if resultado_validacion:
        msg, cmp = resultado_validacion
        contexto.update({"borrador_pendiente": datos, "campo_esperado": cmp})
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, msg)
        return

    contexto["campo_esperado"] = None
    try:
        intencion, fecha = datos["intencion"], datos.get("fecha_operacion", fecha_mensaje)
        if intencion == "REGISTRO_DIARIO":
            r = await asyncio.to_thread(inventario.registrar_registro_diario, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, entradas=datos.get("entradas_revuelto", []), resultados=datos.get("items", []), merma_kg=datos.get("merma_kg", 0))
            salida = f"✅ Guardado: {fecha}."
        elif intencion == "VENTA_DESPACHO":
            r = await asyncio.to_thread(inventario.registrar_venta_multiple, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, items=datos.get("items", []), cliente=datos.get("cliente"), cliente_documento=datos.get("cliente_documento"), cliente_telefono=datos.get("cliente_celular"), cliente_direccion=datos.get("cliente_direccion"), cliente_conductor=datos.get("cliente_conductor"), cliente_conductor_id=datos.get("cliente_conductor_id"), cliente_placa=datos.get("cliente_placa"), cliente_conductor_telefono=datos.get("cliente_conductor_celular"))
            
            pdf_path = os.path.join(tempfile.gettempdir(), f"remision_{usuario_id}_{int(datetime.now(BOGOTA).timestamp())}.pdf")
            await asyncio.to_thread(generar_remision_pdf_archivo, pdf_path, fecha=fecha, cliente=datos.get("cliente", ""), documento=datos.get("cliente_documento"), placa=datos.get("cliente_placa"), conductor=datos.get("cliente_conductor"), id_conductor=datos.get("cliente_conductor_id"), celular_conductor=datos.get("cliente_conductor_celular"), celular=datos.get("cliente_celular"), items=datos.get("items", []), numero_remision=r.get("numero_remision", "SIN-NUMERO"), bodega_id=bodega_id)
            
            salida = f"✅ Venta registrada.\n📄 Remisión generada."
            await enviar_documento_whatsapp(telefono, pdf_path, f"Remision_{fecha}_{datos.get('cliente')}.pdf")
        else:
            # =====================================================================
            # PUNTO 2 (A): BLINDAJE DE VARIABLES LOCALES EN EL ELSE FINAL
            # =====================================================================
            txt_res = "Selecciona una opción del menú:"
            if 'ai' in locals() and getattr(ai, "respuesta_texto", None):
                txt_res = ai.respuesta_texto
                
            contexto["borrador_pendiente"] = datos
            await guardar_contexto(usuario_id, contexto)
            await enviar_botones_whatsapp(telefono, txt_res, [("1", "Ingresar"), ("2", "Ver Stock"), ("3", "Anular")])
            return

        contexto.update({"borrador_pendiente": {}, "campo_esperado": None})
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, salida)
    except Exception as exc:
        await enviar_mensaje_whatsapp(telefono, f"Error de registro: {exc}")


async def procesar_webhook(data: Dict[str, Any]) -> None:
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                # Inyección del interceptor de deduplicación atómico
                if es_mensaje_duplicado(message.get("id", "")):
                    return
                await procesar_un_mensaje(message, value.get("contacts", []))


@app.get("/")
def raiz() -> Dict[str, str]: return {"status": "ok"}

@app.post("/webhook")
async def webhook_whatsapp(request: Request, background_tasks: BackgroundTasks) -> Response:
    cuerpo = await request.body()
    firma = request.headers.get("X-Hub-Signature-256", "")
    if META_APP_SECRET:
        esperada = "sha256=" + hmac.new(META_APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(firma, esperada): return Response("Firma inválida", status_code=403)

    try:
        datos = json.loads(cuerpo)
        background_tasks.add_task(procesar_webhook, datos)
    except Exception as e:
        return Response(f"Error parseando JSON: {e}", status_code=400)
    return Response("EVENT_RECEIVED", status_code=200)

@app.get("/webhook")
async def verificar_webhook(request: Request) -> Response:
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and hmac.compare_digest(p.get("hub.verify_token", ""), VERIFY_TOKEN):
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    return Response("Token inválido", status_code=403)

@app.get("/download/{nombre_archivo}")
async def descargar_documento(nombre_archivo: str) -> Response:
    ruta_archivo = os.path.join(tempfile.gettempdir(), nombre_archivo)
    if not os.path.exists(ruta_archivo): return Response("No encontrado", status_code=404)
    with open(ruta_archivo, 'rb') as f: contenido = f.read()
    return Response(content=contenido, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'})

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
