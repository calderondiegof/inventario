"""Funciones de normalización de texto.

Funciones puras, sin dependencias externas, que normalizan texto para
comparaciones insensibles a mayúsculas, tildes y espacios. Son la base
sobre la que se construyen las búsquedas y deduplicaciones en el dominio
del inventario.
"""
import re
import unicodedata
from typing import Optional, Tuple


def normalizar(texto: str) -> str:
    """Normaliza texto removiendo tildes, diacríticos y espacios extras, en minúsculas.

    Usada como clave de comparación en catálogos, clientes y conductores.
    La semántica es estable desde versiones tempranas del proyecto:
    - Strip + minúsculas
    - NFD + remoción de marcas diacríticas (categoría 'Mn')
    - Colapsa espacios múltiples a uno solo

    Args:
        texto: cadena a normalizar (se acepta None/vacío).

    Returns:
        Cadena normalizada, vacía si la entrada es falsy.
    """
    texto = unicodedata.normalize("NFD", (texto or "").strip().lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto)


# Alias para compatibilidad con parsers.py (consumidores externos).
_normalizar_texto = normalizar
normalizar_nombre = normalizar


def _limpiar_digitos(texto: str) -> str:
    """Extrae solo dígitos del texto (para comparar teléfonos y documentos)."""
    return re.sub(r"\D", "", (texto or "").strip())


def normalizar_digitos(texto: str) -> str:
    """Normaliza un identificador numérico para comparaciones.

    Dado un número de documento o teléfono con o sin formato (guiones,
    puntos, espacios), devuelve solo los dígitos para comparación directa.
    Ejemplo: '300.123.4567' -> '3001234567'
    """
    return _limpiar_digitos(texto)


def normalizar_placa(placa: str) -> str:
    """Normaliza placas eliminando guiones, espacios y convirtiendo a mayúsculas.

    Args:
        placa: placa vehicular (se acepta None/vacío).

    Returns:
        Placa normalizada en mayúsculas sin caracteres especiales.
        Ejemplo: 'abc-123' -> 'ABC123'
    """
    return re.sub(r"[^A-Za-z0-9]", "", (placa or "")).upper()


def extraer_placas(texto: str) -> Tuple[Optional[str], Optional[str]]:
    """Extrae hasta dos placas vehiculares de un texto.

    Busca patrones como 'ABC123' o 'ABC-123' en el texto y devuelve
    la placa principal (cabezal) y la placa del trailer/remolque.

    Args:
        texto: texto que puede contener una o más placas.

    Returns:
        Tupla (placa_principal, placa_trailer) donde cada elemento es
        None si no se encontró.
    """
    if not texto:
        return None, None
    patron = re.compile(
        r"\b([A-Z]{3}[- ]?[0-9]{3})\b",
        re.IGNORECASE
    )
    coincidencias = [
        normalizar_placa(m.group(1))
        for m in patron.finditer(texto)
    ]
    return coincidencias[0] if coincidencias else None, coincidencias[1] if len(coincidencias) > 1 else None