"""Utilidades de parsing de texto (cantidades, bloques de persona, fechas).

Este módulo existe como capa de orquestación que agrupa utilidades de
parsing usadas por los handlers. Las funciones puras de bajo nivel viven
en ``utils/text_normalizer.py`` y ``utils/number_parser.py``.
"""
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.config import BOGOTA
from utils.text_normalizer import normalizar, normalizar_digitos, _normalizar_texto
from utils.number_parser import _parsear_numero

logger = logging.getLogger(__name__)

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


def extraer_fecha_texto(texto: str) -> Optional[str]:
    """Busca una fecha colombiana (dd-mm, dd-mm-aaaa, dd/mm, dd/mm/aaaa o un
    día relativo) en cualquier línea del texto y devuelve su ISO (YYYY-MM-DD),
    o None si no hay ninguna. Se usa como red de seguridad: si la IA no
    extrajo la fecha que el usuario escribió inline en un mensaje nuevo (ej.
    '27/08 Quemé 1354 kg...'), se captura de forma determinista y se evita
    caer por defecto en la fecha del mensaje ('hoy')."""
    for linea in (texto or "").split("\n"):
        ln = linea.strip().lstrip("*-•").strip().lower()
        if not ln:
            continue
        f = parsear_fecha_colombiana(ln)
        if f:
            return f
    return None

# Alias de compatibilidad: 'normalizar_nombre' usado por tests antiguos.
normalizar_nombre = normalizar

_PALABRAS_CLAVE_PROCESO = {"seleccion", "selección", "seleccionar", "seleccionando"}


def _limpiar_nombre_para_busqueda(nombre: str) -> str:
    """Elimina palabras clave de proceso del nombre antes de buscar en el
    catálogo. Devuelve el nombre limpio (minúsculas, sin tildes)."""
    palabras = nombre.strip().lower().split()
    filtradas = [p for p in palabras if normalizar(p) not in _PALABRAS_CLAVE_PROCESO]
    return " ".join(filtradas) if filtradas else " ".join(palabras)

