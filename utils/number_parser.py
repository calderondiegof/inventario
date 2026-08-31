"""Funciones de parseo numérico y de moneda.

Funciones puras, sin dependencias externas, que interpretan números
escritos con formato local (decimal con coma, separadores de miles con
punto o viceversa).
"""
from typing import Optional


def _parsear_numero(texto: str) -> Optional[float]:
    """Parsea un número escrito con formato español o inglés.

    Acepta los formatos más comunes en LATAM:
      - '4120,50'    -> 4120.5   (coma como decimal)
      - '1.250.000'  -> 1250000  (punto como separador de miles)
      - '1,250.50'   -> 1250.5   (mezcla: el último separador es decimal)
      - '1250'       -> 1250.0

    Args:
        texto: cadena con el número a parsear.

    Returns:
        El valor como float, o None si la cadena no es un número válido
        o está vacía.
    """
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


def _parsear_numero_moneda(texto: str) -> Optional[float]:
    """Versión pública compatible con el alias histórico del proyecto.

    Mantener ambos nombres evita romper importadores externos.
    """
    return _parsear_numero(texto)