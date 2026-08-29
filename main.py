import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uvicorn
import tempfile
import re
import unicodedata
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
from services.inventario_service import (
    InventarioServiceConValidacion, normalizar,
    construir_lista_texto_whatsapp, construir_seccion_lista_interactiva,
    borrador_para_nueva_lista, es_lista_materiales,
)
from services.currency_service import obtener_tasa_dolar

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
# Deduplicación de mensajes de WhatsApp con TTL. Meta puede reintentar/
# reentregar un mismo webhook (timeout de DeepSeek/DB, red lenta); esto evita
# que el bot responda (y registre operaciones) varias veces por un mensaje.
# Es un DICT id -> timestamp: cada entrada EXPIRA sola (antes era un set que
# se vaciaba completo al llegar al máximo, dejando pasar reintentos viejos).
_mensajes_whatsapp_procesados: Dict[str, float] = {}
_MAX_MENSAJES_PROCESADOS = 5000
_MENSAJE_TTL_SEGUNDOS = 600.0  # Meta reintenta en minutos; 10 min cubre de sobra


def _mensaje_es_duplicado(mensaje_id: str) -> bool:
    """True si `mensaje_id` ya fue procesado dentro de la ventana TTL.
    Purga primero las entradas expiradas (purga incremental, no reset total)."""
    ahora = time.time()
    expirados = [k for k, ts in _mensajes_whatsapp_procesados.items()
                 if ahora - ts > _MENSAJE_TTL_SEGUNDOS]
    for k in expirados:
        _mensajes_whatsapp_procesados.pop(k, None)
    if len(_mensajes_whatsapp_procesados) > _MAX_MENSAJES_PROCESADOS:
        _mensajes_whatsapp_procesados.clear()
    if mensaje_id in _mensajes_whatsapp_procesados:
        return True
    _mensajes_whatsapp_procesados[mensaje_id] = ahora
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

# Mapa de cada paso del asistente de venta (cliente/conductor) al campo del
# borrador donde se guarda la respuesta. El módulo de cliente y el de conductor
# usan EXACTAMENTE el mismo wizard por pasos; solo se registran en tablas
# distintas (clientes => clientes, conductor => conductores).
VENTA_CAMPOS_PASO = {
    "cliente": "cliente",
    "cliente_documento": "cliente_documento",
    "cliente_direccion": "cliente_direccion",
    "cliente_celular": "cliente_celular",
    "conductor": "cliente_conductor",
    "conductor_id": "cliente_conductor_id",
    "conductor_placa": "cliente_placa",
    "conductor_celular": "cliente_conductor_celular",
}

FECHA_COLOMBIANA = re.compile(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?$")
# Días de la semana en español: se resuelven como el día más reciente (hacia atrás).
DIAS_SEMANA = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}

def parsear_fecha_colombiana(texto: str) -> Optional[str]:
    texto = texto.strip().lower()
    hoy = datetime.now(BOGOTA).date()
    # Fechas relativas comunes, para que respuestas como "hoy"/"ayer" no
    # tengan que pasar por la IA (evita duplicaciones del borrador).
    if texto in {"hoy"}:
        return hoy.isoformat()
    if texto in {"ayer"}:
        return (hoy - timedelta(days=1)).isoformat()
    if texto in {"anteayer"}:
        return (hoy - timedelta(days=2)).isoformat()
    dia_semana = DIAS_SEMANA.get(texto)
    if dia_semana is not None:
        delta = (hoy.weekday() - dia_semana) % 7
        if delta == 0:
            delta = 7  # si es el mismo día de la semana, se asume hace una semana
        return (hoy - timedelta(days=delta)).isoformat()
    m = FECHA_COLOMBIANA.match(texto)
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
    """Elimina explícitamente valores None recursivamente para evitar rechazos en la API de WhatsApp."""
    if isinstance(obj, dict):
        return {k: clean_payload(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, list):
        return [clean_payload(v) for v in obj if v is not None]
    return obj


# Modelos Pydantic para estructura del agente
class ItemMaterial(BaseModel):
    material_nombre: str
    cantidad_kg: float
    precio_unitario: Optional[float] = 0.0


class EntradaRevuelto(BaseModel):
    fuente_nombre: str
    cantidad_kg: float


class RespuestaAgente(BaseModel):
    intencion: Literal["REGISTRO_DIARIO", "ENTRADA_REVUELTO", "SELECCION_REVUELTO", "TRANSFORMACION_MATERIAL", "COMPRA_DIRECTA", "VENTA_DESPACHO", "AJUSTE_INVENTARIO", "CONSULTA", "CONSULTA_INVENTARIO_TOTAL", "VER_MOVIMIENTOS_SELECCION", "REPORTE_POR_FECHA", "OTRO"] = "OTRO"
    fecha_operacion: Optional[str] = None
    entradas_revuelto: List[EntradaRevuelto] = Field(default_factory=list)
    items: List[ItemMaterial] = Field(default_factory=list)
    cantidad_revuelto_procesada: Optional[float] = None
    merma_kg: float = 0.0
    material_origen: Optional[str] = None
    material_merma: Optional[str] = None
    nombre_proceso: Optional[str] = None
    fuente_compra: Optional[str] = None
    cliente: Optional[str] = None
    cliente_documento: Optional[str] = None
    cliente_direccion: Optional[str] = None
    cliente_placa: Optional[str] = None
    cliente_conductor: Optional[str] = None
    cliente_conductor_id: Optional[str] = None  # <--- Incluido para capturar el ID del conductor
    cliente_celular: Optional[str] = None
    cliente_conductor_celular: Optional[str] = None  # <--- Nuevo: Celular del conductor
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


# =====================================================================
# HELPERS REUTILIZABLES
# =====================================================================

def _telefono_limpio(destino: str) -> str:
    """Deja solo los dígitos del número, como espera la API de WhatsApp."""
    return re.sub(r"\D", "", str(destino))


def _payload_base_whatsapp(destino: str, tipo: str) -> dict:
    """Base común de todos los payloads de la API de WhatsApp."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _telefono_limpio(destino),
        "type": tipo,
    }


# Sub-botones del menú de consulta de inventario
SUB_MENU_INVENTARIO = [
    ("inv_total", "Inventario total"),
    ("inv_movs", "Ver movimientos"),
    ("inv_hoy", "Reporte de hoy"),
]


async def enviar_reporte_diario(telefono: str, bodega_id: int, message: Dict[str, Any],
                                dias_atras: int = 0, fecha: Optional[str] = None) -> None:
    """Envía el reporte diario de la bodega. dias_atras=0 → 'hoy', 1 → 'ayer'.
    Si se pasa `fecha` (ISO) se usa esa fecha explícita (flujo Reporte por fecha)."""
    if not fecha:
        fecha = fecha_local_mensaje(message)
        if dias_atras:
            fecha = (datetime.fromisoformat(fecha).date() - timedelta(days=dias_atras)).isoformat()
    reporte = await asyncio.to_thread(inventario.obtener_reporte_diario_texto, bodega_id, fecha)
    await enviar_mensaje_whatsapp(telefono, reporte)


async def enviar_grafico_inventario(telefono: str, bodega_id: int) -> None:
    """Genera y envía la gráfica de stock de la bodega (reporte_grafico)."""
    url = await asyncio.to_thread(generar_y_subir_grafico_stock, bodega_id)
    if url:
        await enviar_imagen_whatsapp(telefono, url, f"Inventario de la bodega {bodega_id}")
    else:
        await enviar_mensaje_whatsapp(telefono, "No hay datos para generar el gráfico.")


async def iniciar_inventario_total(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Submenú de 'Inventario Total': botones para elegir informe en texto o gráfico."""
    contexto["borrador_pendiente"] = {}
    contexto["campo_esperado"] = None
    contexto["accion_pendiente"] = {}
    await guardar_contexto(usuario_id, contexto)
    await enviar_botones_whatsapp(
        telefono, "¿Cómo deseas ver el Inventario Total?",
        [("inv_txt", "Ver informe texto"), ("inv_graf", "Ver informe Grafico")],
    )


async def iniciar_reporte_por_fecha(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Flujo 'Reporte de Hoy' con fecha opcional: pide la fecha (DD/MM/AAAA)
    y ofrece el botón rápido 'Hoy'."""
    contexto["borrador_pendiente"] = {}
    contexto["campo_esperado"] = None
    contexto["accion_pendiente"] = {"tipo": "reporte_fecha"}
    await guardar_contexto(usuario_id, contexto)
    await enviar_botones_whatsapp(
        telefono,
        "Por favor, ingresa la fecha del reporte que deseas consultar "
        "(formato DD/MM/AAAA) o presiona el botón para consultar hoy.",
        [("inv_hoy_hoy", "📅 Hoy")],
    )


async def enviar_inventario_total(telefono: str, bodega_id: int) -> None:
    """Envía el resumen con todos los saldos de la bodega, ordenados
    alfabéticamente (obtener_saldos_bodega ya devuelve la lista ordenada)."""
    saldos = await asyncio.to_thread(inventario.obtener_saldos_bodega, bodega_id)
    if not saldos:
        await enviar_mensaje_whatsapp(telefono, f"No hay stock registrado en la Bodega #{bodega_id}.")
        return
    total_kg = sum(x["saldo_kg"] for x in saldos)
    lineas = [f"📦 Inventario actual — Bodega #{bodega_id}", ""]
    lineas += [f"• {x['material']}: {x['saldo_kg']:,.2f} kg" for x in saldos]
    lineas += ["", f"*Total inventario: {total_kg:,.2f} kg*"]
    await enviar_mensaje_whatsapp(telefono, "\n".join(lineas))


async def pedir_movimientos_material(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Activa el flujo interactivo para consultar movimientos de un material.
    Envía un List Message con TODOS los materiales del catálogo activo; la
    selección (button/list reply llega como texto = nombre exacto) reengancha
    con el estado `movimientos_material`, que resuelve por coincidencia
    exacta. Fallback: si no hay catálogo, pregunta por texto."""
    contexto["accion_pendiente"] = {"tipo": "movimientos_material"}
    contexto["borrador_pendiente"] = {}
    contexto["campo_esperado"] = None
    await guardar_contexto(usuario_id, contexto)
    materiales = sorted(inventario.catalogo_materiales.values(), key=lambda m: m.nombre)
    if materiales:
        await enviar_lista_whatsapp(
            telefono,
            "Selecciona el material para ver sus movimientos:",
            "Ver materiales",
            [(m.nombre, m.nombre, m.tipo_material) for m in materiales],
            titulo_lista="Materiales",
        )
    else:
        await enviar_mensaje_whatsapp(telefono, "¿De qué material deseas ver los movimientos?")


# Frases que activan el flujo de aprobación de Contabilidad (Orden de Salida
# -> Remisión Aprobada). Se comparan contra el texto normalizado por
# _normalizar_texto (minúsculas SIN acentos), por lo que las variantes con
# tilde de este conjunto quedan cubiertas por las equivalentes sin tilde.
TRIGGERS_ORDENES_SALIDA = {
    "ver ordenes de salida", "ordenes de salida",
    "aprobar remisiones", "aprobar remision",
    "aprobar ordenes", "aprobar ordenes de salida",
}


def _normalizar_texto(texto: str) -> str:
    """Normaliza la entrada del usuario para comparaciones exactas:
    trim + minúsculas + REMOCIÓN de acentos/diacríticos (NFD + descarte de
    combining). Así 'órdenes', 'ÓRDENES' y 'ordenes' coinciden sin mantener
    variantes duplicadas en los sets de triggers."""
    t = unicodedata.normalize("NFD", (texto or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.strip().lower()


def _parsear_numero(texto: str) -> Optional[float]:
    """Parsea un número escrito con formato español o inglés:
    '4120,50' -> 4120.5 | '1.250.000' -> 1250000 | '1,250.50' -> 1250.5.
    Devuelve None si no es un número válido."""
    t = (texto or "").strip().replace(" ", "").replace("$", "")
    if not t:
        return None
    if "," in t and "." in t:
        # Ambos separadores: el último es el decimal.
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


async def preparar_flujo_valorizacion(telefono: str, usuario_id: int, bodega_id: int,
                                      contexto: Dict[str, Any], remision: Dict[str, Any]) -> str:
    """Paso 2 del flujo de Contabilidad, compartido por el menú interactivo y
    el código directo: valida que la remisión sea una Orden de Salida aprobable,
    consulta la bodega/país, sugiere la tasa del dólar (currency_service) y
    prepara el contexto para el paso 3 (valor del dólar + precios por kilo).
    Devuelve el texto de respuesta para el usuario."""
    numero = remision["numero"]
    if (remision.get("estado") or "").upper() != "ORDEN_SALIDA":
        contexto["accion_pendiente"] = {}
        return (f"La remisión {numero} está en estado {remision.get('estado')}; "
                f"solo se aprueban Órdenes de Salida.")
    if not remision.get("movimientos"):
        contexto["accion_pendiente"] = {}
        return f"La Orden {numero} no tiene materiales registrados."
    # Info de la bodega: país/moneda para la tasa del dólar.
    bodegas = await asyncio.to_thread(lambda: supabase.table("bodegas")
                                      .select("*").eq("id", bodega_id).limit(1).execute())
    filas_b = getattr(bodegas, "data", None) or [{}]
    info = (filas_b[0] if filas_b else {}) or {}
    pais = info.get("pais") or "Colombia"
    moneda = info.get("moneda") or ""
    tasa = await obtener_tasa_dolar(pais=pais, moneda=moneda)
    tasa_txt = f"{tasa:,.2f}" if tasa else "no disponible (podrás escribirla manualmente)"
    items = [
        {"movimiento_id": m["id"],
         "material_nombre": (m.get("materiales") or {}).get("nombre", "Material"),
         "cantidad_kg": abs(float(m["cantidad_kg"]))}
        for m in remision["movimientos"]
    ]
    contexto["accion_pendiente"] = {
        "tipo": "espera_valor_dolar_dia",
        "remision_id": remision["id"],
        "numero": numero, "pais": pais, "moneda": moneda,
        "tasa_sugerida": tasa, "items": items, "precios": {},
    }
    await guardar_contexto(usuario_id, contexto)
    return (
        f"Orden de Salida {numero} — {len(items)} material(es).\n"
        f"💵 Tasa sugerida del dólar ({pais}): {tasa_txt}\n\n"
        f"Ingrese el Valor_dolar_dia a fijar en la remisión:"
    )


async def iniciar_aprobacion_orden_salida(telefono: str, usuario_id: int,
                                          bodega_id: int, contexto: Dict[str, Any],
                                          numero_directo: Optional[str] = None) -> None:
    """Flujo de Contabilidad, paso 1: consulta las últimas 3 remisiones en
    estado 'ORDEN_SALIDA' de la bodega y muestra el menú de selección con
    botones interactivos de WhatsApp.

    Si se pasa `numero_directo` (ej. 'REM_1001' desde el código 'OS-1001'),
    se omite el menú y se salta directo a la valorización de esa orden."""
    if numero_directo:
        remision = await asyncio.to_thread(inventario.obtener_remision, numero_directo)
        if not remision:
            await enviar_mensaje_whatsapp(
                telefono, f"No encontré la Orden '{numero_directo}'. "
                          f"Escribe 'ver ordenes de salida' para ver las pendientes.")
            return
        logger.info(f"Código directo de Orden de Salida: {numero_directo} "
                    f"(remision_id={remision['id']}, estado={remision.get('estado')})")
        respuesta = await preparar_flujo_valorizacion(telefono, usuario_id, bodega_id, contexto, remision)
        await enviar_mensaje_whatsapp(telefono, respuesta)
        return
    ordenes = await asyncio.to_thread(inventario.obtener_ordenes_salida, bodega_id, 3)
    logger.info(f"Resultado consulta Supabase remisiones (ORDEN_SALIDA, bodega={bodega_id}): {ordenes}")
    if not ordenes:
        await enviar_mensaje_whatsapp(
            telefono, "No hay Órdenes de Salida pendientes de valoración en tu bodega.")
        return
    contexto["accion_pendiente"] = {
        "tipo": "orden_salida_menu",
        "ordenes": [{"remision_id": o["id"], "numero": o["numero"]} for o in ordenes],
    }
    await guardar_contexto(usuario_id, contexto)
    lista = "\n".join(f"• {o['numero']} — fecha {o.get('fecha_operacion') or 'n/d'}" for o in ordenes)
    await enviar_botones_whatsapp(
        telefono,
        f"Órdenes de Salida pendientes de valoración:\n\n{lista}\n\nSelecciona la que deseas aprobar:",
        [(o["numero"], o["numero"]) for o in ordenes[:3]],
    )


async def inferir_datos_ia(usuario: Dict[str, Any], bodega_id: int, fecha_mensaje: str,
                           borrador: Dict[str, Any], texto: str) -> Optional[Dict[str, Any]]:
    """Pregunta al agente (DeepSeek) que interprete el mensaje y lo fusiona con el borrador.
    Devuelve None si la interpretación falló."""
    # REINTENTOS: si el mensaje es una lista de selección de materiales, la
    # lista anterior del borrador se SOBRESCRIBE (no se concatena), evitando
    # registrar el doble de materiales cuando el usuario reintenta tras un error.
    borrador = borrador_para_nueva_lista(borrador, texto)
    try:
        ai = await llamar_deepseek(
            prompt_agente(usuario=usuario["nombre"], bodega_id=bodega_id,
                          fecha_mensaje=fecha_mensaje, borrador=borrador),
            texto,
        )
    except Exception as exc:
        logger.warning(f"⚠️ IA (DeepSeek) no disponible o falló la interpretación: {exc}")
        return None
    datos = fusionar_borrador(borrador, ai)
    # Safety net: si la IA colocó en `merma_kg` la cantidad de un material
    # comercializable del catálogo, reclasifíquila a `items` como resultado de
    # selección. Solo 'basura'/'tierra' (o materiales BRUTO / no encontrados)
    # deben permanecer en `merma_kg`.
    datos = _reclasificar_merma_erronea(texto, datos)
    return datos


# Palabras que no forman parte del nombre canónico de un material y deben
# eliminarse antes de buscar en el catálogo (ej. "Seleccion arreglo carter"
# → "arreglo carter"). No incluimos "arreglo" porque forma parte de nombres
# canónicos como "Arreglo Cobre y Bronce".
_PALABRAS_CLAVE_PROCESO = {"seleccion", "selección", "seleccionar", "seleccionando"}


def _limpiar_nombre_para_busqueda(nombre: str) -> str:
    """Elimina palabras clave de proceso del nombre antes de buscar en el
    catálogo. Devuelve el nombre limpio (minúsculas, sin tildes)."""
    palabras = nombre.strip().lower().split()
    filtradas = [p for p in palabras if normalizar(p) not in _PALABRAS_CLAVE_PROCESO]
    return " ".join(filtradas) if filtradas else " ".join(palabras)


def _reclasificar_merma_erronea(texto: str, datos: Dict[str, Any]) -> Dict[str, Any]:
    """Safety net: si la IA colocó en ``merma_kg`` (o simplemente omitió) la
    cantidad de un material comercializable del catálogo, reclasifíquila a
    ``items`` como resultado de selección. También corrige el caso en que
    la IA duplica cantidades (material en ``items`` Y en ``merma_kg`` al
    mismo tiempo), recalculando ``merma_kg`` desde el texto.

    Reglas:
    - Materiales no BRUTO del catálogo → ``items`` (Resultado de selección).
    - Fuentes como 'Cooperativa', 'Pesca', etc. → se omiten.
    - Materiales BRUTO ('Basura', 'Tierra') o nombres no reconocidos → ``merma_kg``.
    - Solo 'basura' / 'tierra' deben permanecer en ``merma_kg``.
    """
    if datos.get("intencion") not in ("SELECCION_REVUELTO", "REGISTRO_DIARIO", "TRANSFORMACION_MATERIAL"):
        return datos
    if not inventario:
        return datos

    merma_actual = float(datos.get("merma_kg") or 0)

    # Re-parsea el texto del usuario en busca de pares "material cantidad".
    lineas = re.split(r"[\n,;]+", texto.strip())
    pares: List[Tuple[str, float]] = []
    for linea in lineas:
        linea_limpia = linea.strip().lstrip("*-•").strip()
        par = parsear_material_cantidad(linea_limpia)
        if par:
            pares.append((par[0], par[1]))

    if not pares:
        return datos

    items_actuales = list(datos.get("items", []))

    # Pre-computa las claves canónicas ya presentes en items para evitar
    # duplicados (incluso cuando el AI usó un nombre sinónimo como
    # "Rechazo grueso" en lugar de "Arreglo Carter").
    existing_keys: set = set()
    for i in items_actuales:
        existing_mat = inventario.obtener_material_por_nombre(i.get("material_nombre", ""))
        if existing_mat:
            existing_keys.add(normalizar(existing_mat.nombre))

    correct_merma = 0.0
    cantidades_a_mover = 0.0
    items_modificados = False

    for nombre, cantidad in pares:
        # Omitir fuentes (no son materiales).
        if inventario.obtener_fuente_por_nombre(nombre):
            continue

        # Intenta el nombre original y luego el nombre limpio (sin palabras
        # clave de proceso como 'seleccion').
        mat = inventario.obtener_material_por_nombre(nombre)
        if mat is None:
            mat = inventario.obtener_material_por_nombre(
                _limpiar_nombre_para_busqueda(nombre)
            )

        if mat and mat.tipo_material != "BRUTO":
            # Material comercializable → debe estar en items, NUNCA en merma.
            mat_key = normalizar(mat.nombre)
            if mat_key not in existing_keys:
                items_actuales.append({
                    "material_nombre": mat.nombre,
                    "cantidad_kg": cantidad,
                    "precio_unitario": 0.0,
                })
                existing_keys.add(mat_key)
                cantidades_a_mover += cantidad
                items_modificados = True
        else:
            # Basura / tierra / no encontrado → va a merma_kg.
            correct_merma += cantidad

    # --- Corrección 1: materiales faltantes en items ---
    if items_modificados:
        datos["items"] = items_actuales
        # Recalcula merma desde cero: solo basura/tierra deben quedar.
        datos["merma_kg"] = max(0.0, correct_merma)
        # Si recalculamos merma, invalida cantidad_revuelto_procesada para
        # que el servicio lo recupere como resultados + merma.
        datos.pop("cantidad_revuelto_procesada", None)
        return datos

    # --- Corrección 2: duplicación (material en items Y en merma) ---
    # La IA puso el mismo material en items y en merma_kg. Recalcula merma
    # basándose en el texto: solo basura/tierra van a merma.
    if abs(correct_merma - merma_actual) > 0.01:
        datos["merma_kg"] = max(0.0, correct_merma)
        datos.pop("cantidad_revuelto_procesada", None)

    return datos


# =====================================================================
# FUNCIÓN CENTRALIZADA ÚNICA PARA WHATSAPP (SOLUCIÓN DEFINITIVA)
# =====================================================================
async def enviar_mensaje_whatsapp_json(payload: dict) -> None:
    """
    Función centralizada única para enviar JSON a la API de WhatsApp.
    Mantiene la URL oficial protegida con try-except para evitar caídas
    y limpia de forma estricta las variables para evitar errores de Render.
    """
    if not http_client:
        logger.error("❌ http_client no inicializado")
        return

    # Limpieza absoluta de variables para evitar textos corruptos en producción
    phone_id = str(PHONE_NUMBER_ID).strip().replace('"', '').replace("'", "")
    token_limpio = str(WHATSAPP_TOKEN).strip().replace('"', '').replace("'", "")
    
    # URL oficial y segura (Evita problemas de certificados SSL)
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {token_limpio}",
        "Content-Type": "application/json"
    }
    
    cleaned = clean_payload(payload)
    destino = cleaned.get("to", "Desconocido")
    
    logger.info(f"📤 Enviando payload seguro a {destino}")
    
    try:
        # Petición controlada usando el cliente global
        response = await http_client.post(url, json=cleaned, headers=headers)
        
        if response.status_code == 401:
            logger.error("❌ Error 401: El token de administrador de Meta no es válido o expiró.")
            return
            
        response.raise_for_status()
        logger.info(f"✅ Respuesta WhatsApp API: {response.status_code}")
        
    except httpx.ReadTimeout:
        logger.error(f"⏳ Tiempo de espera agotado (Timeout) con Meta API para el destino: {destino}")
    except httpx.ReadError as exc:
        logger.error(f"📡 Error de red temporal en Render (Evitando caída del servidor): {exc}")
    except httpx.HTTPStatusError as exc:
        logger.error(f"💥 Meta API devolvió un error de estado {exc.response.status_code}: {exc.response.text}")
    except Exception as e:
        logger.error(f"⚠️ Error inesperado controlado en el envío de WhatsApp: {e}")

async def enviar_mensaje_whatsapp(destino: str, texto: str) -> None:
    payload = _payload_base_whatsapp(destino, "text")
    payload["text"] = {"body": str(texto)[:4096]}
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_botones_whatsapp(destino: str, texto: str, opciones: List[tuple]) -> None:
    botones = [{"type": "reply", "reply": {"id": id_, "title": titulo[:20]}} for id_, titulo in opciones[:3]]
    payload = _payload_base_whatsapp(destino, "interactive")
    payload["interactive"] = {
        "type": "button",
        "body": {"text": texto},
        "action": {"buttons": botones},
    }
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_lista_whatsapp(destino: str, texto: str, titulo_boton: str,
                                filas: List[tuple], titulo_lista: str = "Opciones") -> None:
    """Envía un catálogo/listado de materiales por WhatsApp con el método que
    respete el límite de la API.

    - Si hay <=10 elementos: Interactive List Message (máx 10 filas totales;
      superarlo dispara el error 400 #131009 de Meta). Cada fila es
      (id, titulo, [descripcion]); al elegir un ítem, el webhook entrega su `id`
      en interactive.list_reply.id.
    - Si hay >10 elementos (ej. 30+ materiales): NO se usa el tipo 'list'; se
      envía un mensaje de TEXTO normal con viñetas ordenado alfabéticamente
      (construir_lista_texto_whatsapp). El nombre del material (title) queda
      disponible para que el usuario lo escriba y reenganche el flujo."""
    filas = list(filas)
    if len(filas) > 10:
        nombres = [str(f[1]) for f in filas]
        await enviar_mensaje_whatsapp(destino,
                                      construir_lista_texto_whatsapp(nombres, titulo=titulo_lista))
        return
    sections = construir_seccion_lista_interactiva(filas, titulo_lista=titulo_lista)
    payload = _payload_base_whatsapp(destino, "interactive")
    payload["interactive"] = {
        "type": "list",
        "body": {"text": texto},
        "action": {
            "button": titulo_boton[:20],
            "sections": sections,
        },
    }
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_imagen_whatsapp(destino: str, url_imagen: str, leyenda: str) -> None:
    payload = _payload_base_whatsapp(destino, "image")
    payload["image"] = {"link": url_imagen, "caption": leyenda}
    await enviar_mensaje_whatsapp_json(payload)

async def enviar_documento_whatsapp(destino: str, ruta_archivo: str, nombre_documento: str = "documento.pdf") -> None:
    if not http_client:
        logger.error("❌ http_client no inicializado")
        return

    logger.info(f"📄 Enviando documento a {destino}: {ruta_archivo}")

    try:
        if not os.path.exists(ruta_archivo):
            logger.error(f"❌ Archivo no encontrado: {ruta_archivo}")
            return

        try:
            url_documento = await subir_archivo_supabase(ruta_archivo, nombre_documento)
        except Exception as e:
            logger.warning(f"⚠️ No se pudo subir a Supabase: {e}, intentando con servidor local")
            base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
            url_documento = f"{base_url}/download/{os.path.basename(ruta_archivo)}"

        payload = _payload_base_whatsapp(destino, "document")
        payload["document"] = {
            "link": url_documento,
            "filename": nombre_documento,
        }
        await enviar_mensaje_whatsapp_json(payload)

    except Exception as e:
        logger.error(f"❌ Error enviando documento: {e}")


async def subir_archivo_supabase(ruta_archivo: str, nombre_documento: str) -> str:
    if not supabase or not http_client:
        raise Exception("Supabase no configurado")

    try:
        with open(ruta_archivo, 'rb') as f:
            contenido = f.read()

        timestamp = int(datetime.now(BOGOTA).timestamp())
        ruta_storage = f"remisiones/{timestamp}_{nombre_documento}"

        supabase.storage.from_("documentos").upload(
            ruta_storage,
            contenido,
            {"content-type": "application/pdf"}
        )

        logger.info(f"✅ Archivo subido a Supabase: {ruta_storage}")

        url_publica = supabase.storage.from_("documentos").get_public_url(ruta_storage)
        return url_publica

    except Exception as e:
        logger.error(f"❌ Error subiendo a Supabase: {e}")
        raise
        
def prompt_agente(*, usuario: str, bodega_id: int, fecha_mensaje: str, borrador: Dict[str, Any]) -> str:
    materiales = [
        {"nombre": x.nombre, "tipo": x.tipo_material, "comercializable": x.es_comercializable}
        for x in inventario.catalogo_materiales.values()
    ]
    fuentes = [x.nombre for x in inventario.catalogo_fuentes.values()]
    return f'''Eres el extractor de datos de un inventario de reciclaje. Devuelve SOLO un objeto JSON válido, sin Markdown.

Usuario: {usuario}; bodega: {bodega_id}; fecha local real del mensaje: {fecha_mensaje}.
Materiales permitidos: {json.dumps(materiales, ensure_ascii=False)}.
Fuentes permitidas: {json.dumps(fuentes, ensure_ascii=False)}.
Borrador de conversación previo: {json.dumps(borrador, ensure_ascii=False)}.

Reglas de negocio:
1. "Cooperativa", "Pesca", "Planta", "Corrientes" y "Compras" son fuentes, no materiales. Un bloque "Materiales" con esas líneas representa ENTRADA_REVUELTO: cada línea entra como material Revuelto desde su fuente.
2. Un bloque "Material seleccionado" es SELECCION_REVUELTO. Sus materiales aprovechables van a `items`. REGLA CLAVE: SI el nombre de un material coincide con alguno de los 'Materiales permitidos' listados arriba, DEBE ir a `items` como 'Resultado de selección de Revuelto'. NUNCA a `merma_kg`. ÚNICAMENTE 'basura', 'tierra', 'basura y tierra' (o expresiones que claramente sean residuos no aprovechables) van a `merma_kg`. Materiales como 'arreglo carter', 'carter', 'latón', 'cobre', 'bronce', 'aluminio', etc. son materiales del catálogo y van a `items`, NUNCA a `merma_kg`. La cantidad de Revuelto procesada es resultados + merma, salvo que el usuario indique otra cantidad explícita que debe coincidir. Si en EL MISMO mensaje están los bloques "Materiales" y "Material seleccionado", usa REGISTRO_DIARIO y conserva ambos bloques. IMPORTANTE: Si el usuario ingresa SOLO basura/merma sin materiales aprovechables (ej. "Basura 50"), es SELECCION_REVUELTO válido: `items` vacío, `merma_kg` con el valor. IMPORTANTE: Si el mensaje es solo "material cantidad" (uno o varios) y no queda claro si es una entrada directa, una transformación/selección de Revuelto, o una salida/venta (no menciona fuente, no dice "venta"/"despacho"/"compra", no dice "Revuelto"), NO asumas cuál es: usa intencion "OTRO", conserva los materiales y cantidades del usuario en `items`, y deja `respuesta_texto` vacío (el sistema pregunta por ti).
3. "Compra de ..." es COMPRA_DIRECTA: entra directamente el material indicado, nunca Revuelto. Usa fuente_compra "Compras" si no se menciona proveedor y existe esa fuente.
4. "Venta" o "despacho" es VENTA_DESPACHO. Admite muchos materiales en `items`. Extrae:
   - cliente (nombre)
   - cliente_documento (documento/ID del cliente)
   - cliente_direccion (dirección del cliente)
   - cliente_celular (teléfono del cliente)
   - cliente_placa (placa del vehículo si se menciona)
   - cliente_conductor (nombre del conductor si se menciona)
   Para registrar la venta, el cliente debe quedar completo: nombre, documento, dirección y celular, ADEMÁS de la placa y el conductor del vehículo. Si falta alguno de estos datos, NO registres nada: en `respuesta_texto` pregunta puntualmente solo por los que falten. Conserva los datos que ya se hayan dado en el borrador. Para la fecha, aplica la regla 9: no la asumas.
5. Para días relativos, calcula la fecha más reciente en o antes de la fecha real del mensaje. Ejemplo: si fecha real es domingo 2026-08-17, "jueves" es 2026-08-13. Si el usuario dice "hoy", usa la fecha real del mensaje; si dice "ayer", usa el día anterior a esa fecha. El usuario también puede escribir la fecha en formato colombiano dd-mm-aaaa o dd-mm (ej. "13-08-2026" o "13-08"); si solo da día y mes, asume el año actual.
6. Conserva y fusiona los datos útiles del borrador. Si falta un dato indispensable, deja la lista vacía o el campo nulo y formula una pregunta precisa en respuesta_texto. Nunca inventes nombres ni cantidades.
7. Acepta listas escritas como "Material - cantidad". Normaliza tildes y espacios solo para encontrar el nombre canónico del catálogo; devuelve el nombre exacto del catálogo.
8. Si el mensaje habla de "inventario inicial", "ajuste de inventario", "ingreso de inventario" o corrección de stock, y lista materiales con sus kilos que NO sean de las fuentes Cooperativa/Pesca/Planta/Corrientes, usa la intención AJUSTE_INVENTARIO. Cada línea va directo a `items` (material_nombre + cantidad_kg); no se necesita fuente ni pasar por Revuelto.
9. NUNCA asumas la fecha de una operación si el usuario no la mencionó explícitamente, ni siquiera como palabra relativa (hoy, ayer, jueves, etc.). Si el mensaje no menciona ninguna fecha, deja `fecha_operacion` en null y en `respuesta_texto` pregunta: "¿Qué fecha fue esta operación? (por ejemplo: hoy, ayer, o 13-08-2026)". Solo llena `fecha_operacion` cuando el usuario haya dicho una fecha (exacta o relativa) en algún momento de la conversación.
10. Si el "Borrador de conversación previo" ya tiene una intención definida (distinta de OTRO) y el mensaje actual es corto y no describe una operación nueva (por ejemplo: solo un nombre, o datos de contacto como documento/dirección/celular/placa/conductor), es la respuesta a la pregunta pendiente. En ese caso usa la MISMA intención del borrador (no OTRO) y extrae lo que puedas del mensaje hacia los campos que faltaban.

11. TRANSFORMACION_MATERIAL es para procesos que parten de un material que NO es Revuelto: quema/tratamiento de un semilimpio (ej. "Quemar 1000 kg de Cable → 600 kg de Cable Quemado + 400 kg de Basura") o selección técnica/desmonte de un semilimpio (ej. "Arreglar 1000 kg de Arreglo Carter → 500 kg Carter + 200 kg Chatarra + 50 kg Cable + 50 kg Arreglo Difícil + 250 kg Basura"). Poblá `material_origen` con el material semilimpio de entrada, cada producto en `items`, y la basura/tierra en `merma_kg`. La cantidad procesada debe ser 100% del origen (se descuenta del semilimpio). Si el origen ES 'Revuelto', usa SELECCION_REVUELTO (no esta intención).

12. CONSULTAS DE INVENTARIO (no registran nada, solo informan): si el usuario pide ver el inventario total/completo de la bodega (ej. "inventario total", "ver todo el inventario"), usa CONSULTA_INVENTARIO_TOTAL. Si pide ver los movimientos o el historial de un material (ej. "movimientos de cobre", "historial de carter"), usa VER_MOVIMIENTOS_SELECCION y pon el material en `consulta_material` si lo menciono (null si no; el sistema mostrara la lista de materiales). Si pide el reporte de un dia (ej. "reporte de hoy", "reporte del 25-08-2026"), usa REPORTE_POR_FECHA: pon la fecha en `fecha_operacion` SOLO si el usuario la menciono (regla 9); si no la menciono dejala en null y el sistema pedira la fecha con opcion 'Hoy').
Esquema exacto:
{{
  "intencion":"REGISTRO_DIARIO|ENTRADA_REVUELTO|SELECCION_REVUELTO|TRANSFORMACION_MATERIAL|COMPRA_DIRECTA|VENTA_DESPACHO|AJUSTE_INVENTARIO|CONSULTA|CONSULTA_INVENTARIO_TOTAL|VER_MOVIMIENTOS_SELECCION|REPORTE_POR_FECHA|OTRO",
  "fecha_operacion":"YYYY-MM-DD|null",
  "entradas_revuelto":[{{"fuente_nombre":"string","cantidad_kg":0}}],
  "items":[{{"material_nombre":"string","cantidad_kg":0,"precio_unitario":0}}],
  "cantidad_revuelto_procesada":0,
  "merma_kg":0,
  "material_origen":"string|null",
  "material_merma":"string|null",
  "nombre_proceso":"string|null",
  "fuente_compra":"string|null",
  "cliente":"string|null",
  "cliente_documento":"string|null",
  "cliente_direccion":"string|null",
  "cliente_placa":"string|null",
  "cliente_conductor":"string|null",
  "cliente_celular":"string|null",
  "consulta_material":"string|null",
  "respuesta_texto":"string"
}}'''


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
    
    # Acumulación segura de materiales sin sobrescribir (Regla clave)
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

    # Acumulación segura de entradas de revuelto: se fusionan por fuente
    # (sumando cantidades) en lugar de duplicarlas si la IA vuelve a devolver
    # fuentes que ya estaban en el borrador.
    if "entradas_revuelto" in datos:
        existentes_rev = {e["fuente_nombre"].lower(): e for e in resultado.get("entradas_revuelto", [])}
        for entrada in datos["entradas_revuelto"]:
            fuente_key = entrada["fuente_nombre"].lower()
            if fuente_key in existentes_rev:
                existentes_rev[fuente_key]["cantidad_kg"] += entrada.get("cantidad_kg", 0)
            else:
                existentes_rev[fuente_key] = entrada
        resultado["entradas_revuelto"] = list(existentes_rev.values())
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

    datos.setdefault("items", [])
    datos.setdefault("entradas_revuelto", [])
    datos.setdefault("merma_kg", 0.0)
    datos.setdefault("fuente_compra", None)
    datos.setdefault("cliente", None)
    datos.setdefault("cliente_documento", None)
    datos.setdefault("cliente_direccion", None)
    datos.setdefault("cliente_placa", None)
    datos.setdefault("cliente_conductor", None)
    datos.setdefault("cliente_celular", None)

    if intento in ("ENTRADA_REVUELTO", "REGISTRO_DIARIO") and not datos.get("entradas_revuelto"):
        return "Indica las fuentes y los kilos de Revuelto, por ejemplo: Cooperativa 500.", "entradas_revuelto"
    if intento in ("REGISTRO_DIARIO", "COMPRA_DIRECTA", "VENTA_DESPACHO", "AJUSTE_INVENTARIO") and not datos.get("items"):
        return "Indica los materiales y kilos que se deben registrar.", "items"
    if intento == "SELECCION_REVUELTO" and not datos.get("items") and not datos.get("merma_kg", 0):
        return "Indica los materiales seleccionados o la cantidad de basura a descontar del Revuelto.", "items"
    if intento == "TRANSFORMACION_MATERIAL":
        if not datos.get("material_origen"):
            return "Indica qué material se transforma (ejemplo: Cable, Arreglo Carter).", "material_origen"
        if not datos.get("items") and not datos.get("merma_kg", 0):
            return "Indica los materiales resultantes o la cantidad de merma de la transformación.", "items"
    
    # Máquina de estados estricta para VENTA_DESPACHO.
    # Cliente y conductor comparten el mismo wizard por pasos (módulos idénticos):
    # se pide un campo a la vez y, si el conductor ya existe en la tabla
    # "conductores", solo se solicitan los campos que aún le falten.
    if intento == "VENTA_DESPACHO":
        # ---- Módulo CLIENTE ----
        if not datos.get("cliente"):
            return "Indica el nombre del cliente, por favor.", "cliente"
        if not cliente_existe:
            if not datos.get("cliente_documento"):
                return "Es un cliente nuevo. Indica su documento / cédula.", "cliente_documento"
            if not datos.get("cliente_direccion"):
                return "Indica la dirección del cliente.", "cliente_direccion"
            if not datos.get("cliente_celular"):
                return "Indica el celular del cliente.", "cliente_celular"
        # ---- Módulo CONDUCTOR (mismo wizard que cliente) ----
        if not datos.get("cliente_conductor"):
            return "Indica el nombre del conductor, por favor.", "conductor"
        if not datos.get("cliente_conductor_id"):
            return "Indica el ID / cédula del conductor.", "conductor_id"
        if not datos.get("cliente_placa"):
            return "Indica la placa (patente) del vehículo.", "conductor_placa"
        if not datos.get("cliente_conductor_celular"):
            return "Indica el celular del conductor.", "conductor_celular"

    if intento == "COMPRA_DIRECTA" and not datos.get("fuente_compra"):
        datos["fuente_compra"] = "Compras"
    if intento in ("REGISTRO_DIARIO", "ENTRADA_REVUELTO", "SELECCION_REVUELTO", "TRANSFORMACION_MATERIAL", "COMPRA_DIRECTA", "VENTA_DESPACHO", "AJUSTE_INVENTARIO") and not datos.get("fecha_operacion"):
        return "¿Qué fecha fue esta operación? Puedes responder 'hoy', 'ayer', un día de la semana, o una fecha exacta (dd-mm-aaaa).", "fecha_operacion"
    return None


async def guardar_contexto(usuario_id: int, contexto: Dict[str, Any]) -> None:
    await asyncio.to_thread(lambda: supabase.table("usuarios").update({"contexto_operacion": contexto}).eq("id", usuario_id).execute())


def construir_mensaje_seleccion(r: Dict[str, Any], fecha: str,
                                omitidos: Optional[List[str]] = None) -> str:
    """Plantilla ESTRICTA de la confirmación de selección por WhatsApp.

    Función SÍNCRONA a propósito: no realiza ningún `await`, por lo que se
    llama directamente en el dispatch de `procesar_un_mensaje`. (Si fuera
    `async def`, un llamado sin `await` enviaría un objeto coroutine como
    mensaje en vez del texto real.)

    Formato base:
      ✅ Selección registrada: {n} resultado(s), merma {merma:.2f} kg,
      revuelto: -{total:.0f} kg, fecha {fecha}.

    Si hay ítems que no pudieron registrarse (no encontrados en catálogo),
    se añade la sección de alerta — los ítems omitidos JAMÁS se ignoran en
    silencio."""
    msg = (f"✅ Selección registrada: {len(r['registros']) - 1} resultado(s), "
           f"merma {r['merma_kg']:.2f} kg, revuelto: -{r['revuelto_descontado']:.0f} kg, "
           f"fecha {fecha}.")
    if omitidos:
        detalle = "\n".join(f"- {o} kg (Material no encontrado en el catálogo)"
                            for o in omitidos)
        msg += ("\n\n⚠️ **Atención:** Los siguientes ítems no se pudieron "
                f"registrar y fueron ignorados:\n{detalle}")
    return msg


def consolidar_seleccion(datos: Dict[str, Any], texto: str) -> None:
    """Post-procesado del borrador de SELECCION_REVUELTO (muta `datos`):

    1. Si el mensaje del usuario es una LISTA de materiales (viñetas o líneas
       'Material Cantidad'), se resuelve de forma DETERMINISTA con
       resolver_lista_materiales: cada línea se consume una vez y los ítems no
       encontrados se reportan en datos['materiales_omitidos'] (nada se
       omite en silencio, evitando que la IA simplifique/recorte frases).
    2. Clasificación de merma: cualquier item cuyo material sea de tipo MERMA
       (ej. 'Basura') se mueve a merma_kg — la basura nunca se registra como
       material comercializable.

    Debe llamarse con contexto de catálogo cargado (inventario.catalogo_materiales).
    """
    if datos.get("intencion") != "SELECCION_REVUELTO":
        return
    omitidos: List[str] = []
    if es_lista_materiales(texto):
        items, no_encontrados, merma_lista = inventario.resolver_lista_materiales(texto)
        datos["items"] = items
        datos["merma_kg"] = float(datos.get("merma_kg") or 0) + merma_lista
        omitidos = list(no_encontrados)
    else:
        # Ruta IA: separar items de tipo MERMA hacia merma_kg.
        items = datos.get("items") or []
        vendibles: List[Dict[str, Any]] = []
        for it in items:
            mat = inventario.obtener_material_por_nombre(it.get("material_nombre") or "")
            if mat and (mat.tipo_material or "").upper() == "MERMA":
                datos["merma_kg"] = float(datos.get("merma_kg") or 0) + float(it.get("cantidad_kg") or 0)
            else:
                vendibles.append(it)
        datos["items"] = vendibles
    datos["materiales_omitidos"] = omitidos



async def regenerar_y_enviar_pdf_remision(telefono: str, bodega_id: int, numero: str) -> str:
    """Regenera el PDF de una remisión EXISTENTE conservando el mismo número
    correlativo (no se genera uno nuevo) y lo envía por WhatsApp.

    Devuelve un mensaje de confirmación o de error.
    """
    datos = await asyncio.to_thread(inventario.obtener_datos_pdf_remision, numero)
    numero_remision = datos["numero_remision"]
    try:
        nombre_pdf = f"remision_{numero_remision}_{int(datetime.now(BOGOTA).timestamp())}.pdf"
        pdf_path = os.path.join(tempfile.gettempdir(), nombre_pdf)
        cliente = datos.get("cliente") or {}
        conductor = datos.get("conductor") or {}
        await asyncio.to_thread(
            generar_remision_pdf_archivo,
            pdf_path,
            fecha=datos.get("fecha_operacion") or "",
            cliente=cliente.get("nombre", "") or "",
            documento=cliente.get("identificacion"),
            direccion=cliente.get("direccion"),
            celular=cliente.get("telefono"),
            placa=conductor.get("placa"),
            conductor=conductor.get("nombre"),
            id_conductor=conductor.get("identificacion"),
            celular_conductor=conductor.get("telefono"),
            items=datos.get("items", []),
            numero_remision=numero_remision,
            bodega_id=bodega_id,
            # El PDF se adapta al estado: 'ORDEN_SALIDA' sin precios,
            # 'APROBADA' completo con conversiones a dólar.
            estado=datos.get("estado"),
            vr_dolar_dia=datos.get("vr_dolar_dia"),
        )
        await enviar_documento_whatsapp(
            destino=telefono,
            ruta_archivo=pdf_path,
            nombre_documento=f"Remision_Corregida_{numero_remision}.pdf",
        )
    except Exception as e:
        logger.error(f"❌ Error regenerando PDF de {numero_remision}: {e}")
        raise
    return f"✅ Remisión {numero_remision} corregida. PDF regenerado con el mismo número y enviado por WhatsApp."


async def procesar_un_mensaje(message: Dict[str, Any], contactos: List[Dict[str, Any]]) -> None:
    logger.info(f"🔄 Iniciando procesamiento de mensaje: {message}")
    tipo_mensaje = message.get("type")
    # 1) Extracción del texto entrante con cadena de fallbacks INDEPENDIENTE
    #    del campo 'type' (texto, botón interactivo o lista interactiva), y
    #    normalización única al inicio: así ninguna rama posterior de la
    #    función puede usar `texto`/`texto_normalizado` sin definir
    #    (evita el UnboundLocalError en los triggers de comandos directos).
    texto_recibido = (
        message.get("text", {}).get("body", "")
        or message.get("interactive", {}).get("button_reply", {}).get("id", "")
        or message.get("interactive", {}).get("list_reply", {}).get("id", "")
    )
    texto_normalizado = _normalizar_texto(texto_recibido)
    texto = texto_recibido.strip()
    logger.info(f"Texto recibido procesado: '{texto_normalizado}'")
    if tipo_mensaje == "interactive":
        interactivo = message.get("interactive", {})
        if interactivo.get("type") not in {"button_reply", "list_reply"}:
            logger.warning(f"⚠️ Tipo interactivo no soportado: {interactivo.get('type')}")
    if not texto:
        logger.warning(f"⚠️ Mensaje sin texto soportado: tipo={tipo_mensaje}")
        return
    telefono = str(message.get("from", "")).replace("+", "")
    logger.info(f"📱 Teléfono: {telefono}, Texto: {texto}")
    if not telefono or not texto:
        logger.warning(f"❌ Teléfono o texto vacío - Abortando")
        return
    usuarios = await asyncio.to_thread(lambda: supabase.table("usuarios").select("*,bodegas(nombre)").eq("telefono_whatsapp", telefono).execute())
    if not usuarios.data:
        await enviar_mensaje_whatsapp(telefono, "Acceso denegado: número no registrado.")
        return
    usuario = usuarios.data[0]
    usuario_id, bodega_id = usuario["id"], usuario.get("bodega_asignada_id")
    if not bodega_id:
        await enviar_mensaje_whatsapp(telefono, "Tu usuario no tiene una bodega asignada.")
        return
    contexto = usuario.get("contexto_operacion") or {}
    if texto.lower() in {"cancelar", "limpiar", "reiniciar"}:
        contexto["borrador_pendiente"] = {}
        contexto["accion_pendiente"] = {}
        contexto["campo_esperado"] = None
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "Operación cancelada.")
        return

    accion = contexto.get("accion_pendiente") or {}
    if accion.get("tipo"):
        respuesta_texto = None
        try:
            if accion["tipo"] == "espera_remision_modo":
                eleccion = texto.strip().lower()
                if eleccion in {"anular", "anular_rem", "anular rem", "anulacion", "anulación", "1"}:
                    contexto["accion_pendiente"] = {"tipo": "espera_numero_remision", "modo": "anular"}
                    respuesta_texto = "Anulación. ¿Qué remisión deseas anular? (ejemplo: REM_112)"
                elif eleccion in {"corregir", "corregir_rem", "corregir rem", "correccion", "corrección", "2"}:
                    contexto["accion_pendiente"] = {"tipo": "espera_numero_remision", "modo": "corregir"}
                    respuesta_texto = "Corrección. ¿Qué remisión deseas corregir? (ejemplo: REM_112)"
                else:
                    respuesta_texto = "Responde 'anular' o 'corregir', o usa los botones."
            elif accion["tipo"] == "espera_numero_remision":
                remision = await asyncio.to_thread(inventario.obtener_remision, texto)
                if not remision:
                    respuesta_texto = f"No encontré la remisión '{texto}'. Verifica el número."
                    contexto["accion_pendiente"] = {}
                elif accion.get("modo") == "corregir":
                    contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": remision["numero"], "modo": "corregir"}
                    respuesta_texto = (
                        f"Corrección de la remisión {remision['numero']}. ¿Qué deseas corregir?\n"
                        "1. Material (cantidad)\n2. Cliente\n3. Finalizar y generar PDF"
                    )
                else:
                    contexto["accion_pendiente"] = {"tipo": "espera_alcance", "numero": remision["numero"], "modo": "anular"}
                    respuesta_texto = f"¿Deseas anular TODA la remisión {remision['numero']}? (sí/no)"
            elif accion["tipo"] == "espera_alcance":
                if texto.strip().lower() in {"si", "sí"}:
                    r = await asyncio.to_thread(inventario.anular_remision_completa, accion["numero"], usuario_id)
                    respuesta_texto = f"Remisión {r['numero']} anulada por completo ({r['lineas_anuladas']} línea(s)). El stock fue devuelto."
                    contexto["accion_pendiente"] = {}
                elif texto.strip().lower() == "no":
                    contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
                    respuesta_texto = (
                        f"Corrección de la remisión {accion['numero']}. ¿Qué deseas corregir?\n"
                        "1. Material (cantidad)\n2. Cliente\n3. Finalizar y generar PDF"
                    )
                else:
                    respuesta_texto = "Responde sí o no, por favor."
            elif accion["tipo"] == "espera_material":
                if texto.strip().lower() in {"listo", "terminar", "finalizar", "fin", "generar pdf", "finalizar y generar pdf"}:
                    try:
                        respuesta_texto = await regenerar_y_enviar_pdf_remision(telefono, bodega_id, accion["numero"])
                    except Exception as exc:
                        respuesta_texto = f"Correcciones guardadas, pero no se pudo regenerar el PDF: {exc}"
                    contexto["accion_pendiente"] = {}
                else:
                    par = parsear_material_cantidad(texto)
                    if not par:
                        respuesta_texto = "No entendí. Escribe así: Material cantidad (ejemplo: Carter 3500). O escribe *finalizar* cuando termines."
                    else:
                        material_nombre, cantidad = par
                        try:
                            r = await asyncio.to_thread(
                                inventario.anular_o_actualizar_linea, numero=accion["numero"],
                                material_nombre=material_nombre, cantidad_kg=cantidad, usuario_id=usuario_id,
                            )
                        except ValueError as exc:
                            respuesta_texto = str(exc)
                            contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": accion["numero"], "modo": "corregir"}
                        else:
                            if r["accion"] == "anulada":
                                respuesta_texto = (f"Se anuló {r['material']} ({r['cantidad']:,.2f} kg) de la remisión {accion['numero']}. "
                                                   "Stock devuelto. Puedes seguir corrigiendo o escribe *finalizar*.")
                                contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
                            else:
                                contexto["accion_pendiente"] = {
                                    "tipo": "espera_confirmacion_actualizacion", "numero": accion["numero"],
                                    "movimiento_id": r["movimiento_id"], "material": r["material"],
                                    "cantidad_nueva": r["cantidad_nueva"],
                                }
                                respuesta_texto = (
                                    f"Ese detalle no existe. En la remisión {accion['numero']}, {r['material']} está en "
                                    f"{r['cantidad_actual']:,.2f} kg. ¿Deseas actualizarlo a {r['cantidad_nueva']:,.2f} kg? (sí/no)"
                                )
            elif accion["tipo"] == "espera_confirmacion_actualizacion":
                if texto.strip().lower() in {"si", "sí"}:
                    await asyncio.to_thread(
                        inventario.actualizar_cantidad_linea,
                        movimiento_id=accion["movimiento_id"], nueva_cantidad_kg=accion["cantidad_nueva"],
                    )
                    respuesta_texto = (f"{accion['material']} actualizado a {accion['cantidad_nueva']:,.2f} kg en la remisión {accion['numero']}. "
                                       "Puedes seguir corrigiendo (Material/Cliente) o escribe *finalizar*.")
                    contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
                elif texto.strip().lower() == "no":
                    contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": accion["numero"], "modo": "corregir"}
                    respuesta_texto = "Digite los datos que desea modificar, o escribe *finalizar*."
                else:
                    respuesta_texto = "Responde sí o no, por favor."
            elif accion["tipo"] == "corregir_opciones":
                eleccion = texto.strip().lower()
                numero = accion["numero"]
                if eleccion in {"1", "material", "materiales"}:
                    contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": numero, "modo": "corregir"}
                    respuesta_texto = f"Corrección de materiales de {numero}. Escribe: Material cantidad (ejemplo: Carter 3500). O *finalizar* para terminar."
                elif eleccion in {"2", "cliente"}:
                    rem = await asyncio.to_thread(inventario.obtener_remision, numero)
                    if not rem or not rem.get("cliente_id"):
                        respuesta_texto = f"La remisión {numero} no tiene un cliente asociado. Elige otra opción."
                        contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": numero, "modo": "corregir"}
                    else:
                        contexto["accion_pendiente"] = {
                            "tipo": "correccion_rem_cliente", "numero": numero,
                            "cliente_id": rem["cliente_id"], "modo": "corregir",
                        }
                        respuesta_texto = "Escribe los datos del cliente a corregir (ejemplo: telefono 3001234567, direccion Calle 10 #5-20)."
                elif eleccion in {"3", "finalizar", "listo", "terminar", "fin", "generar pdf"}:
                    try:
                        respuesta_texto = await regenerar_y_enviar_pdf_remision(telefono, bodega_id, numero)
                    except Exception as exc:
                        respuesta_texto = f"Correcciones guardadas, pero no se pudo regenerar el PDF: {exc}"
                    contexto["accion_pendiente"] = {}
                else:
                    respuesta_texto = "Elige: 1. Material, 2. Cliente, 3. Finalizar."
            elif accion["tipo"] == "correccion_rem_cliente":
                campos = parsear_campos_cliente(texto)
                if not campos:
                    respuesta_texto = "No entendí los datos. Ejemplo: telefono 3001234567, direccion Calle 10 #5-20."
                else:
                    try:
                        await asyncio.to_thread(inventario.actualizar_cliente, accion["cliente_id"], campos)
                    except Exception as exc:
                        respuesta_texto = str(exc)
                        contexto["accion_pendiente"] = {"tipo": "correccion_rem_cliente", "numero": accion["numero"], "cliente_id": accion["cliente_id"], "modo": "corregir"}
                    else:
                        respuesta_texto = (f"Datos del cliente actualizados en la remisión {accion['numero']}. "
                                           "Puedes seguir corrigiendo (Material/Cliente) o escribe *finalizar*.")
                        contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
            elif accion["tipo"] == "correccion_cliente_nombre":
                cliente = await asyncio.to_thread(inventario.obtener_cliente_por_nombre, texto)
                if not cliente:
                    respuesta_texto = f"No encontré ningún cliente llamado '{texto}'."
                    contexto["accion_pendiente"] = {}
                else:
                    contexto["accion_pendiente"] = {"tipo": "correccion_cliente_datos", "cliente_id": cliente["id"], "cliente_nombre": cliente["nombre"]}
                    respuesta_texto = "Escriba los datos que desea corregir (ejemplo: telefono 3001234567, direccion Calle 10 #5-20)."
            elif accion["tipo"] == "correccion_cliente_datos":
                campos = parsear_campos_cliente(texto)
                if not campos:
                    respuesta_texto = "No entendí los datos. Ejemplo: telefono 3001234567, direccion Calle 10 #5-20."
                else:
                    await asyncio.to_thread(inventario.actualizar_cliente, accion["cliente_id"], campos)
                    respuesta_texto = f"Datos de {accion['cliente_nombre']} actualizados."
                    contexto["accion_pendiente"] = {}
            elif accion["tipo"] == "reporte_fecha":
                # Flujo "Reporte por fecha": fecha escrita (DD/MM/AAAA,
                # DD-MM-AAAA o DD-MM) o el botón rápido 📅 Hoy (llega como
                # 'inv_hoy_hoy', ya ruteado antes, pero por si llega aquí).
                if texto_normalizado == "inv_hoy_hoy":
                    contexto["accion_pendiente"] = {}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_reporte_diario(telefono, bodega_id, message)
                else:
                    fecha_parseada = parsear_fecha_colombiana(texto)
                    if not fecha_parseada:
                        respuesta_texto = ("No entendí la fecha. Usa el formato DD/MM/AAAA "
                                           "(ejemplo: 25/08/2026) o presiona el botón 'Hoy'.")
                    else:
                        if fecha_parseada > fecha_local_mensaje(message):
                            respuesta_texto = "Esa fecha es futura. Indica una fecha válida."
                        else:
                            contexto["accion_pendiente"] = {}
                            await guardar_contexto(usuario_id, contexto)
                            await enviar_reporte_diario(telefono, bodega_id, message,
                                                        fecha=fecha_parseada)
                            return
            elif accion["tipo"] == "orden_salida_menu":
                # Paso 2: el usuario (Contabilidad) selecciona una Orden de Salida
                # (por botón interactivo —llega su id, que es el número— o tipeándolo).
                # obtener_remision es tolerante a variantes: '101', 'REM_101',
                # 'OS-1001', 'os 1001', etc.
                remision = await asyncio.to_thread(inventario.obtener_remision, texto.strip())
                if not remision:
                    respuesta_texto = f"No encontré la Orden '{texto.strip()}'. Escribe el número o usa los botones."
                else:
                    respuesta_texto = await preparar_flujo_valorizacion(
                        telefono, usuario_id, bodega_id, contexto, remision)
            elif accion["tipo"] == "espera_valor_dolar_dia":
                # Paso 3: captura del valor del dólar del día.
                valor = _parsear_numero(texto)
                if valor is None or valor <= 0:
                    respuesta_texto = ("Valor inválido. Ingrese el valor del dólar del día "
                                       "(ejemplo: 4120,50) o escriba 'cancelar'.")
                else:
                    accion["vr_dolar_dia"] = valor
                    accion["tipo"] = "captura_precio_material"
                    accion["indice"] = 0
                    primer = accion["items"][0]
                    respuesta_texto = (
                        f"Valor del dólar fijado: {valor:,.2f}\n\n"
                        f"Ingrese el precio por kilo (en moneda local) para "
                        f"{primer['material_nombre']} ({primer['cantidad_kg']:,.2f} kg):"
                    )
            elif accion["tipo"] == "captura_precio_material":
                # Paso 4: bucle de precios por kilo, ítem por ítem; al terminar
                # se ejecuta la aprobación final (paso 5) vía RPC.
                precio = _parsear_numero(texto)
                if precio is None or precio < 0:
                    respuesta_texto = ("Precio inválido. Ingrese el precio por kilo en moneda local "
                                       "(ejemplo: 3500) o escriba 'cancelar'.")
                else:
                    actual = accion["items"][accion["indice"]]
                    accion["precios"][str(actual["movimiento_id"])] = precio
                    accion["indice"] += 1
                    if accion["indice"] < len(accion["items"]):
                        sig = accion["items"][accion["indice"]]
                        respuesta_texto = (
                            f"Precio registrado: {precio:,.2f}/kg.\n\n"
                            f"Ingrese el precio por kilo (en moneda local) para "
                            f"{sig['material_nombre']} ({sig['cantidad_kg']:,.2f} kg):"
                        )
                    else:
                        # Último precio capturado: se pregunta cómo imprimir los
                        # valores en el PDF antes de aprobar (paso 5).
                        accion["tipo"] = "seleccion_modo_pdf"
                        respuesta_texto = (
                            "¿Cómo deseas que se impriman los valores en el PDF de la Remisión?\n"
                            "1. Moneda local\n"
                            "2. Moneda dólares\n"
                            "3. Ambas monedas\n"
                            "4. Sin valores"
                        )
            elif accion["tipo"] == "seleccion_modo_pdf":
                # Paso 5: elección del formato de valores del PDF final y
                # aprobación de la remisión (RPC) + envío del PDF por WhatsApp.
                modos = {
                    "1": "MONEDA_LOCAL", "moneda local": "MONEDA_LOCAL",
                    "2": "DOLARES", "dolares": "DOLARES", "dólares": "DOLARES",
                    "moneda dolares": "DOLARES", "moneda dólares": "DOLARES",
                    "3": "AMBAS", "ambas": "AMBAS", "ambas monedas": "AMBAS",
                    "4": "SIN_VALORES", "sin valores": "SIN_VALORES",
                }
                modo = modos.get(texto.strip().lower())
                if not modo:
                    respuesta_texto = ("Opción inválida. Responde 1 (Moneda local), 2 (Moneda dólares), "
                                       "3 (Ambas monedas) o 4 (Sin valores).")
                else:
                    await asyncio.to_thread(
                        inventario.aprobar_remision_con_precios,
                        accion["remision_id"], accion["vr_dolar_dia"],
                        # Claves tal cual (str en BD real con UUID; el servicio
                        # las normaliza a str para el JSONB). NO convertir a
                        # int: con PKs UUID lanzaría ValueError.
                        dict(accion["precios"]),
                    )
                    datos_pdf = await asyncio.to_thread(
                        inventario.obtener_datos_pdf_remision, accion["numero"])
                    total_local = sum(
                        float(i.get("cantidad_kg") or 0) * float(i.get("precio_unitario") or 0)
                        for i in datos_pdf.get("items", [])
                    )
                    total_dolar = total_local / accion["vr_dolar_dia"] if accion["vr_dolar_dia"] else 0.0
                    numero_rem = datos_pdf.get("numero_remision") or accion["numero"]
                    etiqueta_modo = {
                        "MONEDA_LOCAL": "Moneda local",
                        "DOLARES": "Moneda dólares",
                        "AMBAS": "Ambas monedas",
                        "SIN_VALORES": "Sin valores",
                    }[modo]
                    try:
                        nombre_pdf = f"remision_aprobada_{usuario_id}_{int(datetime.now(BOGOTA).timestamp())}.pdf"
                        pdf_path = os.path.join(tempfile.gettempdir(), nombre_pdf)
                        cliente = datos_pdf.get("cliente") or {}
                        conductor = datos_pdf.get("conductor") or {}
                        await asyncio.to_thread(
                            generar_remision_pdf_archivo,
                            pdf_path,
                            fecha=datos_pdf.get("fecha_operacion") or "",
                            cliente=cliente.get("nombre", "") or "",
                            documento=cliente.get("identificacion"),
                            direccion=cliente.get("direccion"),
                            celular=cliente.get("telefono"),
                            placa=conductor.get("placa"),
                            conductor=conductor.get("nombre"),
                            id_conductor=conductor.get("identificacion"),
                            celular_conductor=conductor.get("telefono"),
                            items=datos_pdf.get("items", []),
                            numero_remision=numero_rem,
                            bodega_id=bodega_id,
                            estado="APROBADA",
                            vr_dolar_dia=accion["vr_dolar_dia"],
                            modo_valores=modo,
                        )
                        logger.info(f"📄 Remisión Aprobada generada ({modo}): {pdf_path}")
                        await enviar_documento_whatsapp(
                            destino=telefono,
                            ruta_archivo=pdf_path,
                            nombre_documento=f"Remision_Aprobada_{numero_rem}.pdf",
                        )
                        respuesta_texto = (
                            f"✅ Remisión Aprobada #{numero_rem}.\n"
                            f"💵 Valor dólar día: {accion['vr_dolar_dia']:,.2f}\n"
                            f"🧾 Total valorizado: {total_local:,.2f} {accion.get('moneda') or ''}"
                            f" (≈ US$ {total_dolar:,.2f})\n"
                            f"🖨️ PDF generado en modo: {etiqueta_modo}."
                        )
                    except Exception as e:
                        logger.error(f"❌ Error generando/enviando PDF de Remisión Aprobada: {e}")
                        respuesta_texto = (
                            f"✅ Remisión {numero_rem} APROBADA y precios guardados "
                            f"({len(accion['precios'])} precio(s)).\n"
                            f"⚠️ No se pudo generar o enviar el PDF. Intenta corregir la remisión "
                            f"para regenerarlo."
                        )
                    contexto["accion_pendiente"] = {}
            elif accion["tipo"] == "movimientos_material":
                if texto.lower().strip() in {"cancelar", "salir", "menu"}:
                    contexto["accion_pendiente"] = {}
                    respuesta_texto = "Operación cancelada."
                else:
                    texto_buscado = texto.lower().strip()

                    # Se buscan TODOS los materiales cuyo nombre contenga lo escrito;
                    # así, si hay varias coincidencias, no se adivina: se confirma.
                    coincidencias = [
                        mat for mat in inventario.catalogo_materiales.values()
                        if texto_buscado in mat.nombre.lower()
                    ]

                    # Sin coincidencias por subcadena: se intenta el buscador
                    # exacto/tolerante por si fue un error de tipeo o un sinónimo.
                    if not coincidencias:
                        try:
                            posible = inventario.obtener_material_por_nombre(texto)
                            coincidencias = [posible] if posible else []
                        except Exception:
                            coincidencias = []

                    if len(coincidencias) == 1:
                        material_encontrado = coincidencias[0]
                    elif len(coincidencias) > 1:
                        # AMBIGUO: se pide al usuario que confirme con el número
                        # o el nombre exacto de la lista.
                        nombre_unicos = list(dict.fromkeys(m.nombre for m in coincidencias))
                        contexto["accion_pendiente"] = {
                            "tipo": "confirmar_material",
                            "candidatos": nombre_unicos,
                            "texto_buscado": texto,
                        }
                        await guardar_contexto(usuario_id, contexto)
                        lista = "\n".join(f"{i+1}. {n}" for i, n in enumerate(nombre_unicos))
                        await enviar_mensaje_whatsapp(
                            telefono,
                            f"Varios materiales coinciden con '{texto}'. "
                            f"Escribe el número del que quieres o el nombre exacto:\n\n{lista}",
                        )
                        return
                    else:
                        material_encontrado = None

                    if not material_encontrado:
                        respuesta_texto = f"No encontré el material '{texto}'. Intenta de nuevo o escribe *cancelar*."
                        contexto["accion_pendiente"] = {"tipo": "movimientos_material"}
                    else:
                        contexto["accion_pendiente"] = {"tipo": "movimientos_rango", "material": material_encontrado.nombre}
                        await guardar_contexto(usuario_id, contexto)
                        await enviar_botones_whatsapp(
                            telefono, f"¿Qué rango de fechas quieres ver para {material_encontrado.nombre}?",
                            [("todo", "Todo el historial"), ("rango", "Elegir fechas")],
                        )
                        return
            elif accion["tipo"] == "confirmar_material":
                candidatos = accion.get("candidatos", [])
                eleccion = texto.strip().lower()
                elegido = None
                if eleccion.isdigit():
                    idx = int(eleccion) - 1
                    if 0 <= idx < len(candidatos):
                        elegido = candidatos[idx]
                else:
                    # Coincidencia por nombre exacto dentro de la lista ofrecida.
                    for c in candidatos:
                        if c.lower() == eleccion:
                            elegido = c
                            break
                    if not elegido:
                        try:
                            mat = inventario.obtener_material_por_nombre(texto)
                            if mat and mat.nombre in candidatos:
                                elegido = mat.nombre
                        except Exception:
                            pass
                if elegido:
                    contexto["accion_pendiente"] = {"tipo": "movimientos_rango", "material": elegido}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_botones_whatsapp(
                        telefono, f"¿Qué rango de fechas quieres ver para {elegido}?",
                        [("todo", "Todo el historial"), ("rango", "Elegir fechas")],
                    )
                    return
                lista = "\n".join(f"{i+1}. {n}" for i, n in enumerate(candidatos))
                respuesta_texto = f"No reconocí '{texto}'. Escribe el número o el nombre exacto:\n\n{lista}"
            elif accion["tipo"] == "movimientos_rango":
                eleccion = texto.strip().lower()
                if eleccion in {"todo", "1"}:
                    resultado = await asyncio.to_thread(
                        inventario.obtener_movimientos_material,
                        bodega_id=bodega_id, material_nombre=accion["material"],
                    )
                    respuesta_texto = formatear_movimientos_material(resultado)
                    contexto["accion_pendiente"] = {}
                elif eleccion in {"rango", "2"}:
                    contexto["accion_pendiente"] = {"tipo": "movimientos_desde", "material": accion["material"]}
                    respuesta_texto = "Fecha desde (dd-mm-aaaa o dd-mm):"
                else:
                    respuesta_texto = "Responde 'todo' o 'rango', o usa los botones."
            elif accion["tipo"] == "movimientos_desde":
                fecha_desde = parsear_fecha_colombiana(texto)
                if not fecha_desde:
                    respuesta_texto = "No entendí la fecha. Usa el formato dd-mm-aaaa o dd-mm (ejemplo: 13-08-2026 o 13-08)."
                else:
                    contexto["accion_pendiente"] = {"tipo": "movimientos_hasta", "material": accion["material"], "fecha_desde": fecha_desde}
                    respuesta_texto = "Fecha hasta (dd-mm-aaaa o dd-mm):"
            elif accion["tipo"] == "movimientos_hasta":
                fecha_hasta = parsear_fecha_colombiana(texto)
                if not fecha_hasta:
                    respuesta_texto = "No entendí la fecha. Usa el formato dd-mm-aaaa o dd-mm (ejemplo: 18-08-2026 o 18-08)."
                else:
                    resultado = await asyncio.to_thread(
                        inventario.obtener_movimientos_material,
                        bodega_id=bodega_id, material_nombre=accion["material"],
                        fecha_desde=accion["fecha_desde"], fecha_hasta=fecha_hasta,
                    )
                    respuesta_texto = formatear_movimientos_material(resultado)
                    contexto["accion_pendiente"] = {}
        except Exception as exc:
            contexto["accion_pendiente"] = {}
            respuesta_texto = f"No pude completar la acción: {exc}. Se canceló, intenta de nuevo."
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, respuesta_texto)
        return
    # Comandos directos por texto
    if texto.lower() in {"anular", "corregir", "anular/corregir rem", "anular rem", "corregir rem", "anular o corregir"}:
        contexto["accion_pendiente"] = {"tipo": "espera_remision_modo"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(
            telefono,
            "¿Deseas ANULAR o CORREGIR una remisión?",
            [("anular_rem", "Anular Remisión"), ("corregir_rem", "Corregir Remisión")],
        )
        return
    if texto.lower() == "corregir cliente":
        contexto["accion_pendiente"] = {"tipo": "correccion_cliente_nombre"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "¿Cuál cliente deseas corregir? (nombre)")
        return
    # Código directo de Orden de Salida (ej. "OS-1001", "os 1001", "OS_1001"):
    # omite el menú y entra directo a la valorización de esa orden.
    m_codigo_os = re.fullmatch(r"os[-_ ]?(\d{1,10})", texto_normalizado)
    if m_codigo_os:
        await iniciar_aprobacion_orden_salida(
            telefono, usuario_id, bodega_id, contexto,
            numero_directo=f"REM_{m_codigo_os.group(1)}",
        )
        return
    if texto_normalizado in TRIGGERS_ORDENES_SALIDA:
        await iniciar_aprobacion_orden_salida(telefono, usuario_id, bodega_id, contexto)
        return
    if texto.lower() in {"ver grafico", "ver gráfico", "reporte visual"}:
        url = await asyncio.to_thread(generar_y_subir_grafico_stock, bodega_id)
        if url:
            await enviar_imagen_whatsapp(telefono, url, f"Inventario de la bodega {bodega_id}")
        else:
            await enviar_mensaje_whatsapp(telefono, "No hay datos para generar el gráfico.")
        return
    # `texto_normalizado` ya viene normalizado desde el inicio de la función.
    if texto_normalizado in {"reporte de hoy", "reporte hoy", "ver reporte de hoy"}:
        await enviar_reporte_diario(telefono, bodega_id, message)
        return
    if texto_normalizado in {"reporte de ayer", "reporte ayer", "ver reporte de ayer"}:
        await enviar_reporte_diario(telefono, bodega_id, message, dias_atras=1)
        return
        

# Despliegue estricto de sub-botones al presionar o escribir "Ver Inventario"
    if texto_normalizado in {"ver_inventario", "ver inventario", "ver saldos", "saldos", "inventario", "2"}:
        contexto["borrador_pendiente"] = {}
        contexto["campo_esperado"] = None
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(telefono, "¿Qué deseas consultar en el inventario?", SUB_MENU_INVENTARIO)
        return

    # 2. Sub-botón: Inventario total
    if texto_normalizado == "inv_total":
        await iniciar_inventario_total(telefono, usuario_id, contexto)
        return

    # 2b. Sub-botones del submenú "Inventario Total" (informe texto / gráfico).
    if texto_normalizado == "inv_txt":
        await enviar_inventario_total(telefono, bodega_id)
        return
    if texto_normalizado == "inv_graf":
        await enviar_grafico_inventario(telefono, bodega_id)
        return

    # 3. Sub-botón: Ver movimientos
    if texto_normalizado == "inv_movs":
        await pedir_movimientos_material(telefono, usuario_id, contexto)
        return

    # 4. Sub-botón: Reporte de hoy
    if texto_normalizado == "inv_hoy":
        await iniciar_reporte_por_fecha(telefono, usuario_id, contexto)
        return

    # 4b. Botón rápido "📅 Hoy" del flujo Reporte por fecha.
    if texto_normalizado == "inv_hoy_hoy":
        await enviar_reporte_diario(telefono, bodega_id, message)
        return

    # Acceso directo por texto para movimientos
    if texto_normalizado in {"movimiento", "movimientos", "ver movimientos", "historial", "movimientos material"}:
        await pedir_movimientos_material(telefono, usuario_id, contexto)
        return

    # Opciones de Menú
    if texto in {"1", "Ingresar Inventario"}:
        contexto["campo_esperado"] = "menu_ingreso"
        await guardar_contexto(usuario_id, contexto)
        submenu_ingreso = [
            ("entrada", "Entrada"),
            ("arreglo", "Seleccion o Arreglo"),
            ("salida", "Salida o venta")
        ]
        await enviar_botones_whatsapp(
            telefono,
            "Selecciona el tipo de movimiento a registrar:",
            submenu_ingreso
        )
        return

    elif texto in {"3", "Anular Inventario"} or texto.lower() in {"anular", "corregir", "anular/corregir rem", "anular rem", "corregir rem", "anular o corregir", "anular/corregir"}:
        contexto["accion_pendiente"] = {"tipo": "espera_remision_modo"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(
            telefono,
            "¿Deseas ANULAR o CORREGIR una remisión?",
            [("anular_rem", "Anular Remisión"), ("corregir_rem", "Corregir Remisión")],
        )
        return

    
    await asyncio.to_thread(inventario.recargar_catalogos)
    fecha_mensaje = fecha_local_mensaje(message)
    borrador_anterior = contexto.get("borrador_pendiente") or {}
    campo_esperado = contexto.get("campo_esperado")

    if campo_esperado in VENTA_CAMPOS_PASO:
        datos = dict(borrador_anterior)
        clave = VENTA_CAMPOS_PASO[campo_esperado]
        valor = texto.strip()
        # Si el usuario incluye la etiqueta del paso (ej. "placa ABC123", "id 1098")
        # se extrae solo el valor mediante el parser de campos; si no, se toma el
        # texto tal cual.
        campos_parseados = parsear_campos_cliente_venta(texto)
        if clave in campos_parseados:
            valor = campos_parseados[clave]
        datos[clave] = valor
    elif campo_esperado in {"tipo_movimiento", "menu_ingreso"}:
        datos = dict(borrador_anterior)
        eleccion = texto.strip().lower()
        if eleccion in {"1", "entrada"}:
            datos["intencion"] = "AJUSTE_INVENTARIO"
        elif eleccion in {"2", "arreglo", "transformacion", "transformación", "seleccion", "selección", "seleccion arreglo"}:
            datos["intencion"] = "SELECCION_REVUELTO"
        elif eleccion in {"3", "salida", "venta", "despacho"}:
            datos["intencion"] = "VENTA_DESPACHO"
        else:
            await enviar_mensaje_whatsapp(telefono, "No entendí. Selecciona: Entrada, Seleccion Arreglo o Salida.")
            return
    elif campo_esperado == "fecha_operacion":
        datos = dict(borrador_anterior)
        fecha_parseada = parsear_fecha_colombiana(texto)
        if fecha_parseada:
            if fecha_parseada > fecha_mensaje:
                await enviar_mensaje_whatsapp(telefono, "Esa fecha es futura. Indica una fecha válida.")
                return
            datos["fecha_operacion"] = fecha_parseada
        else:
            datos = await inferir_datos_ia(usuario, bodega_id, fecha_mensaje, borrador_anterior, texto)
    else:
        datos = await inferir_datos_ia(usuario, bodega_id, fecha_mensaje, borrador_anterior, texto)
    if datos is None:
        # IA no disponible: se purgan los materiales del borrador para que un
        # reintento del usuario no acumule ítems del intento fallido.
        if isinstance(contexto.get("borrador_pendiente"), dict):
            contexto["borrador_pendiente"]["items"] = []
            await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "No pude interpretar el mensaje. Inténtalo nuevamente.")
        return

    # Consolidación de la selección: resolución determinista de listas (nada
    # se omite en silencio) y clasificación de merma (Basura → merma_kg).
    consolidar_seleccion(datos, texto)

    cliente_existente = None
    conductor_existente = None
    if datos.get("intencion") == "VENTA_DESPACHO" and datos.get("cliente"):
        cliente_existente = await asyncio.to_thread(inventario.obtener_cliente_por_nombre, datos["cliente"])
        if cliente_existente:
            datos["cliente_documento"] = datos.get("cliente_documento") or cliente_existente.get("identificacion")
            datos["cliente_direccion"] = datos.get("cliente_direccion") or cliente_existente.get("direccion")
            datos["cliente_celular"] = datos.get("cliente_celular") or cliente_existente.get("telefono")
    if datos.get("intencion") == "VENTA_DESPACHO" and datos.get("cliente_conductor"):
        conductor_existente = await asyncio.to_thread(inventario.obtener_conductor_por_nombre, datos["cliente_conductor"])
        if conductor_existente:
            # Si el conductor ya existe, completar los datos que falten a partir del
            # registro: así solo se piden por pasos los campos que aún no tiene.
            datos["cliente_conductor_id"] = datos.get("cliente_conductor_id") or conductor_existente.get("identificacion")
            datos["cliente_placa"] = datos.get("cliente_placa") or conductor_existente.get("placa")
            datos["cliente_conductor_celular"] = datos.get("cliente_conductor_celular") or conductor_existente.get("telefono")

    if datos.get("intencion") in (None, "OTRO") and datos.get("items"):
        contexto["borrador_pendiente"] = datos
        contexto["campo_esperado"] = "tipo_movimiento"
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(
            telefono, "Selecciona el tipo de movimiento:",
            [("entrada", "Entrada"), ("arreglo", "Seleccion Arreglo"), ("salida", "Salida")],
        )
        return

    resultado_validacion = validar_completitud(datos, fecha_mensaje, cliente_existe=bool(cliente_existente))
    if resultado_validacion:
        mensaje_faltante, campo = resultado_validacion
        contexto["borrador_pendiente"] = datos
        contexto["campo_esperado"] = campo
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, mensaje_faltante)
        return
    # PROTECCIÓN 1 (estado): el borrador se purga ANTES del guardado final.
    # Si llega un duplicado/reintento mientras se escribe en DB, el contexto
    # ya no contiene el payload y la segunda pasada no re-registra nada.
    contexto["campo_esperado"] = None
    contexto["borrador_pendiente"] = {}
    await guardar_contexto(usuario_id, contexto)
    try:
        intencion, fecha = datos["intencion"], datos.get("fecha_operacion", fecha_mensaje)
        if intencion == "CONSULTA":
            material = inventario.obtener_material_por_nombre(datos.get("consulta_material") or "")
            if material:
                salida = f"Stock de {material.nombre}: {inventario.obtener_saldo(bodega_id, material.id):,.2f} kg."
            else:
                saldos = inventario.obtener_saldos_bodega(bodega_id)
                salida = "\n".join(["Inventario actual:"] + [f"- {x['material']}: {x['saldo_kg']} kg" for x in saldos])
        elif intencion == "CONSULTA_INVENTARIO_TOTAL":
            await iniciar_inventario_total(telefono, usuario_id, contexto)
            return
        elif intencion == "VER_MOVIMIENTOS_SELECCION":
            await pedir_movimientos_material(telefono, usuario_id, contexto)
            return
        elif intencion == "REPORTE_POR_FECHA":
            fecha_rep = datos.get("fecha_operacion")
            if fecha_rep:
                await enviar_reporte_diario(telefono, bodega_id, message, fecha=str(fecha_rep))
            else:
                await iniciar_reporte_por_fecha(telefono, usuario_id, contexto)
            return
        elif intencion == "REGISTRO_DIARIO":
            r = await asyncio.to_thread(inventario.registrar_registro_diario, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, entradas=datos.get("entradas_revuelto", []), resultados=datos.get("items", []), merma_kg=datos.get("merma_kg", 0), cantidad_revuelto_procesada=datos.get("cantidad_revuelto_procesada"))
            salida = (f"⚠️ Registro diario duplicado detectado; ya se había guardado hace instantes (merma {r['merma_kg']:,.2f} kg, fecha {fecha})."
                      if r.get("duplicado") else
                      f"Registro diario guardado: {len(r['registros'])} movimientos y merma de {r['merma_kg']:,.2f} kg, fecha {fecha}.")
        elif intencion == "ENTRADA_REVUELTO":
            r = await asyncio.to_thread(inventario.registrar_entrada_revuelto, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, entradas=datos.get("entradas_revuelto", []))
            salida = f"Entrada de Revuelto registrada: {len(r['registros'])} fuente(s), fecha {fecha}."
        elif intencion == "SELECCION_REVUELTO":
            r = await asyncio.to_thread(inventario.registrar_seleccion_revuelto, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, resultados=datos.get("items", []), merma_kg=datos.get("merma_kg", 0), cantidad_revuelto_procesada=datos.get("cantidad_revuelto_procesada"))
            salida = ("⚠️ Selección duplicada: ya se registró hace instantes "
                      f"(revuelto: -{r['revuelto_descontado']:g} kg, fecha {fecha}); no se volvió a registrar."
                      if r.get("duplicado") else
                      construir_mensaje_seleccion(r, fecha, datos.get("materiales_omitidos")))
        elif intencion == "TRANSFORMACION_MATERIAL":
            origen = datos.get("material_origen") or "Revuelto"
            r = await asyncio.to_thread(
                inventario.registrar_transformacion_material,
                bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha,
                material_origen_nombre=origen,
                resultados=datos.get("items", []),
                merma_kg=datos.get("merma_kg", 0),
                cantidad_procesada=datos.get("cantidad_revuelto_procesada"),
                material_merma_nombre=datos.get("material_merma"),
                nombre_proceso=datos.get("nombre_proceso") or "Transformación",
            )
            salida = ("⚠️ Transformación duplicada: ya se registró hace instantes; no se volvió a registrar."
                      if r.get("duplicado") else
                      (f"Transformación registrada desde {r['origen']}: {len(r['registros'])} movimiento(s), "
                       f"merma {r['merma_kg']:,.2f} kg, fecha {fecha}."))
        elif intencion == "COMPRA_DIRECTA":
            r = await asyncio.to_thread(inventario.registrar_compra_directa, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, fuente_nombre=datos["fuente_compra"], items=datos["items"])
            salida = f"Compra registrada: {len(r['registros'])} material(es), fecha {fecha}."
        elif intencion == "AJUSTE_INVENTARIO":
            r = await asyncio.to_thread(inventario.registrar_ajuste_inventario, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, items=datos["items"])
            salida = f"Ajuste de inventario registrado: {len(r['registros'])} material(es), fecha {fecha}."
        elif intencion == "VENTA_DESPACHO":
            r = await asyncio.to_thread(
                inventario.registrar_venta_multiple,
                bodega_id=bodega_id, 
                usuario_id=usuario_id, 
                fecha_operacion=fecha,
                items=datos.get("items", []), 
                cliente=datos.get("cliente"),
                cliente_documento=datos.get("cliente_documento"),
                cliente_telefono=datos.get("cliente_celular"),
                cliente_direccion=datos.get("cliente_direccion"),
                cliente_conductor=datos.get("cliente_conductor"),
                cliente_conductor_id=datos.get("cliente_conductor_id"),
                cliente_placa=datos.get("cliente_placa"),
                cliente_conductor_telefono=datos.get("cliente_conductor_celular"), # <--- Asegúrate de usar esta clave aquí
            )

            pdf_path = None
            numero_orden = r.get("numero_remision", "SIN-NUMERO")
            try:
                nombre_pdf = f"orden_salida_{usuario_id}_{int(datetime.now(BOGOTA).timestamp())}.pdf"
                pdf_path = os.path.join(tempfile.gettempdir(), nombre_pdf)
                conductor_reg = r.get("conductor") or {}
                await asyncio.to_thread(
                    generar_remision_pdf_archivo,
                    pdf_path,
                    fecha=fecha,
                    cliente=datos.get("cliente", "") or (r.get("cliente") or {}).get("nombre", ""),
                    documento=datos.get("cliente_documento") or (r.get("cliente") or {}).get("identificacion"),
                    direccion=datos.get("cliente_direccion") or (r.get("cliente") or {}).get("direccion"),
                    celular=datos.get("cliente_celular") or (r.get("cliente") or {}).get("telefono"),
                    placa=datos.get("cliente_placa") or conductor_reg.get("placa"),
                    conductor=datos.get("cliente_conductor") or conductor_reg.get("nombre"),
                    id_conductor=datos.get("cliente_conductor_id") or conductor_reg.get("identificacion"),
                    celular_conductor=datos.get("cliente_conductor_telefono") or conductor_reg.get("telefono"),
                    items=datos.get("items", []),
                    numero_remision=numero_orden,
                    bodega_id=bodega_id,
                    # Flujo "Orden de Salida -> Remisión Aprobada": la venta nace
                    # sin precios; el PDF es una Orden de Salida SIN valores.
                    estado=r.get("estado") or "ORDEN_SALIDA",
                )
                logger.info(f"📄 Orden de Salida generada: {pdf_path}")
                salida = (
                    f"✅ Orden de Salida #{numero_orden} registrada exitosamente: "
                    f"{len(r['registros'])} material(es).\n"
                    f"📅 Fecha: {fecha}\n"
                    f"⏳ La Orden de Salida queda pendiente de valoración y aprobación "
                    f"por el área de Contabilidad."
                )

                try:
                    await enviar_documento_whatsapp(
                        destino=telefono,
                        ruta_archivo=pdf_path,
                        nombre_documento=f"Orden_de_Salida_{numero_orden}.pdf"
                    )
                except Exception as e:
                    logger.error(f"❌ Error enviando PDF por WhatsApp: {e}")
                    salida += "\n⚠️ Orden de Salida generada pero no se pudo enviar por WhatsApp"

            except Exception as e:
                logger.error(f"❌ Error generando Orden de Salida: {e}")
                salida = f"✅ Venta registrada: {len(r['registros'])} material(es), fecha {fecha}.\n⚠️ No se pudo generar la Orden de Salida."
        else:
            contexto["borrador_pendiente"] = datos
            await guardar_contexto(usuario_id, contexto)
            
            menu_principal = [
                ("1", "Ingresar Inventario"),
                ("2", "Ver Inventario"),
                ("3", "Anular/Corregir Rem")
            ]
            
            await enviar_botones_whatsapp(
                destino=telefono,
                texto=datos.get("respuesta_texto") or "¿Qué operación deseas registrar?",
                opciones=menu_principal
            )
            return
        contexto["borrador_pendiente"] = {}
        contexto["campo_esperado"] = None
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, salida)
    except Exception as exc:
        # ERROR de validación/registro (ej. material no encontrado): se PURGAN
        # los materiales del borrador inmediatamente; si el usuario reenvía la
        # lista corregida, no se concatenan con los del intento fallido.
        logger.error(f"❌ Error registrando la operación: {exc}")
        if isinstance(contexto.get("borrador_pendiente"), dict):
            contexto["borrador_pendiente"]["items"] = []
            await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, f"No registré la operación: {exc}")


async def procesar_webhook(data: Dict[str, Any]) -> None:
    logger.info(f"📥 Procesando webhook: {json.dumps(data, ensure_ascii=False)[:500]}")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                mensaje_id = message.get("id")
                if mensaje_id:
                    if _mensaje_es_duplicado(mensaje_id):
                        logger.info(f"⏭️ Mensaje {mensaje_id} ya procesado (o dentro de la ventana TTL); se omite para evitar duplicados.")
                        continue
                logger.info(f"📨 Mensaje nuevo: {message}")
                try:
                    await procesar_un_mensaje(message, value.get("contacts", []))
                except Exception:
                    # NUNCA silenciar: una excepción aquí (error de PostgREST,
                    # red, bug) deja al bot mudo sin que el operador lo note.
                    logger.exception("❌ Error no controlado procesando el mensaje de WhatsApp")


# Endpoints HTTP / Webhook API
@app.get("/")
def raiz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/test")
def test() -> Dict[str, Any]:
    return {
        "status": "ok",
        "VERIFY_TOKEN_set": "✅" if VERIFY_TOKEN else "❌",
        "WHATSAPP_TOKEN_set": "✅" if WHATSAPP_TOKEN else "❌",
        "PHONE_NUMBER_ID_set": "✅" if PHONE_NUMBER_ID else "❌",
        "META_APP_SECRET_set": "✅" if META_APP_SECRET else "❌",
        "DEEPSEEK_API_KEY_set": "✅" if DEEPSEEK_API_KEY else "❌",
    }


@app.get("/debug")
def debug() -> Dict[str, Any]:
    logger.info("🔍 Endpoint debug llamado")
    return {
        "status": "Servidor funcionando",
        "VERIFY_TOKEN_value": VERIFY_TOKEN[:5] + "..." if VERIFY_TOKEN else "❌ NO CONFIGURADO",
        "PHONE_NUMBER_ID_value": PHONE_NUMBER_ID if PHONE_NUMBER_ID else "❌ NO CONFIGURADO",
        "META_APP_SECRET_configured": bool(META_APP_SECRET),
        "WHATSAPP_TOKEN_configured": bool(WHATSAPP_TOKEN),
    }


@app.get("/webhook")
async def verificar_webhook(request: Request) -> Response:
    p = request.query_params
    logger.info(f"GET /webhook - Parámetros: {dict(p)}")
    logger.info(f"VERIFY_TOKEN configurado: {bool(VERIFY_TOKEN)}")
    logger.info(f"Token recibido: {p.get('hub.verify_token', '(vacío)')}")
    if p.get("hub.mode") == "subscribe" and hmac.compare_digest(p.get("hub.verify_token", ""), VERIFY_TOKEN):
        logger.info("✅ Webhook verificado correctamente")
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    logger.warning("❌ Validación fallida - Token inválido o modo incorrecto")
    return Response("Token inválido", status_code=403)

@app.post("/webhook")
async def webhook_whatsapp(request: Request, background_tasks: BackgroundTasks) -> Response:
    cuerpo = await request.body()
    firma = request.headers.get("X-Hub-Signature-256", "")
    logger.info(f"📨 POST /webhook - Recibido webhook")
    logger.info(f"Firma recibida: {firma[:30] if firma else '(vacía)'}...")
    logger.info(f"Cuerpo: {cuerpo[:300]}...")
    if not META_APP_SECRET:
        # Sin app secret configurado NO se procesa ningún evento: si se aceptara,
        # cualquier POST a esta URL pública podría hacer que el bot enviara
        # mensajes de WhatsApp por sí solo. Es más seguro rechazarlo.
        logger.warning("❌ META_APP_SECRET no configurado: se rechaza el webhook (no se procesa).")
        logger.warning("   El bot NO enviará mensajes de WhatsApp sin firma válida de Meta.")
        return Response("Firma no configurada", status_code=403)
    esperada = "sha256=" + hmac.new(META_APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
    logger.info(f"Validando firma - Esperada: {esperada[:30]}...")
    if not hmac.compare_digest(firma, esperada):
        logger.warning("❌ Firma inválida: se rechaza el evento y NO se envía ningún mensaje.")
        return Response("Firma inválida", status_code=403)
    logger.info("✅ Firma validada")

    try:
        datos = json.loads(cuerpo)
        logger.info(f"✅ JSON parseado correctamente")
        background_tasks.add_task(procesar_webhook, datos)
    except json.JSONDecodeError as e:
        logger.error(f"❌ Error parseando JSON: {e}")
        return Response(f"Error parseando JSON: {e}", status_code=400)

    return Response("EVENT_RECEIVED", status_code=200)


@app.get("/download/{nombre_archivo}")
async def descargar_documento(nombre_archivo: str) -> Response:
    ruta_archivo = os.path.join(tempfile.gettempdir(), nombre_archivo)

    if not os.path.exists(ruta_archivo):
        logger.warning(f"⚠️ Archivo no encontrado: {ruta_archivo}")
        return Response("Archivo no encontrado", status_code=404)

    try:
        with open(ruta_archivo, 'rb') as f:
            contenido = f.read()

        return Response(
            content=contenido,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{nombre_archivo}"'
            }
        )
    except Exception as e:
        logger.error(f"❌ Error descargando archivo: {e}")
        return Response("Error descargando archivo", status_code=500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
