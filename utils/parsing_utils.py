"""Parsing de bloques de texto libre.

- Bloques de persona (nombre, documento, telefono, direccion, placa)
- Sinonimos y frases compuestas para normalizacion
- Deduplicacion de operaciones (huellas)
"""
import re
import hashlib
from typing import Any, Dict, List, Optional

_PATRON_DOCUMENTO = re.compile(
    r"(?:CC|C\.C\.|DNI|NIT|RUT|CI|PP|Pasaporte)\s*[:.\-]?\s*(\d[\d.,\-]*\d|\d+)", re.IGNORECASE)
_PATRON_CELULAR = re.compile(
    r"(?:Cel(?:ular)?|Tel(?:efono)?|Movil|Móvil|WhatsApp|Wsp)\s*[:.\-]?\s*(\+?\d[\d\s\-\(\)]{7,})", re.IGNORECASE)
_PATRON_PLACA = re.compile(r"\b([A-Z]{3}[- ]?[0-9]{3})\b", re.IGNORECASE)
_PATRON_DIRECCION = re.compile(
    r"(?:Dir(?:eccion)?|Direccion|Domicilio)\s*[:.\-]?\s*([^\n]{5,50})", re.IGNORECASE)
_PATRON_NOMENCLATURA = re.compile(
    r"\b(Calle|Cll|Cl|Carrera|Cra|Av|Avenida|Diagonal|Dg|Transversal|Autopista)\s+\d", re.IGNORECASE)

def _normalizar_lineas(texto: str) -> List[str]:
    return [ln.strip() for ln in (texto or "").split("\n") if ln.strip()]

def _tiene_nomenclatura(linea: str) -> bool:
    return bool(_PATRON_NOMENCLATURA.search(linea))

def parsear_bloque_persona(texto: str) -> Dict[str, str]:
    from utils.text_normalizer import normalizar_placa, normalizar_digitos, extraer_placas
    lineas = _normalizar_lineas(texto)
    if not lineas:
        return {"nombre": None, "identificacion": None, "telefono": None, "direccion": None, "placa": None, "placa_trailer": None}
    resultado: Dict[str, Any] = {"nombre": None, "identificacion": None, "telefono": None, "direccion": None, "placa": None, "placa_trailer": None}
    for linea in lineas:
        m = _PATRON_DOCUMENTO.search(linea)
        if m:
            resultado["identificacion"] = normalizar_digitos(m.group(1))
        m = _PATRON_CELULAR.search(linea)
        if m:
            resultado["telefono"] = normalizar_digitos(m.group(1))
        m = _PATRON_PLACA.search(linea)
        if m:
            p1, p2 = extraer_placas(linea)
            if p1 and not resultado["placa"]:
                resultado["placa"] = p1
            if p2 and not resultado["placa_trailer"]:
                resultado["placa_trailer"] = p2
        m = _PATRON_DIRECCION.search(linea)
        if m and not resultado["direccion"]:
            resultado["direccion"] = m.group(1).strip()
    if not resultado["direccion"]:
        for linea in lineas:
            if _tiene_nomenclatura(linea) and not _PATRON_CELULAR.search(linea) and not _PATRON_DOCUMENTO.search(linea):
                resultado["direccion"] = linea
                break
    sin_etiquetas = [
        l for l in lineas
        if not _PATRON_DOCUMENTO.search(l)
        and not _PATRON_CELULAR.search(l)
        and not _PATRON_PLACA.search(l)
        and l != resultado.get("direccion")
    ]
    if sin_etiquetas:
        resultado["nombre"] = sin_etiquetas[0]
    return {k: v or "" for k, v in resultado.items()}

_SINONIMOS: Dict[str, str] = {
    "cable": "Cable", "cable quema": "Cable Quema", "cable quemado": "Cable Quema",
    "alambre": "Cable", "alambre quemado": "Cable Quema",
    "barras": "Barras", "barra": "Barras",
    "lamina": "Laminal", "laminas": "Laminal",
    "chatarra": "Chatarra", "acero": "Acero", "hierro": "Acero",
    "acero inoxidable": "Acero Inoxidable", "inox": "Acero Inoxidable",
    "aluminio": "Aluminio", "bronce": "Bronce",
    "cobre": "Cobre", "zinc": "Zinc", "plomo": "Plomo",
    "carter": "Carter",
    "basura": "Basura", "tierra": "Basura", "merma": "Merma",
}

# Sinónimos PALABRA A PALABRA del dominio del inventario. Se aplican cuando la
# frase completa no es un material del catálogo y soportan materiales compuestos
# de 2+ palabras que repiten la primera palabra a modo de clasificador:
#   'grueso'      -> 'carter'
#   'rechazo'     -> 'arreglo'
# Así 'rechazo grueso' -> 'arreglo carter', 'rechazo cobre' -> 'arreglo cobre',
# 'grueso' -> 'carter', sin recortar la frase a una sola palabra.
_SINONIMOS_PALABRA: Dict[str, str] = {
    "grueso": "carter",
    "rechazo": "arreglo",
    "alambre": "cable",
    "hierro": "acero",
    "tierra": "basura",
}

_FRASES: List[str] = [
    "cable quema", "cable quemado", "alambre quemado",
    "acero inoxidable", "carter de motor",
]

def aplicar_sinonimos(texto: str) -> str:
    t = (texto or "").strip().lower()
    if not t:
        return (texto or "").strip()
    # 1) Frase exacta conocida (frases compuestas del catálogo, ej.
    #    'acero inoxidable' -> 'acero inoxidable'). Se devuelve en minúsculas
    #    para que coincida con las claves normalizadas del catálogo.
    if t in _SINONIMOS:
        return _SINONIMOS[t].lower()
    # 2) Sinónimos palabra a palabra (ej. 'grueso'->'carter', 'rechazo'->'arreglo').
    #    Reemplaza cada palabra de forma independiente, de modo que los
    #    materiales compuestos que repiten la primera palabra ('rechazo grueso',
    #    'rechazo cobre') se normalizan sin perder el resto de la frase.
    palabras = [p for p in t.split() if p]
    if not palabras:
        return (texto or "").strip()
    return " ".join(_SINONIMOS_PALABRA.get(p, p) for p in palabras)

def aplicar_frases(texto: str) -> str:
    resultado = texto
    for frase in _FRASES:
        if frase in resultado:
            resultado = resultado.replace(frase, frase.replace(" ", "_"))
    return resultado

def normalizar_nombre_material(texto: str) -> str:
    return aplicar_sinonimos(aplicar_frases(texto or ""))

_HUELLAS: Dict[str, float] = {}

def _verificar_duplicada(huella: str) -> bool:
    return huella in _HUELLAS

def _registrar_huella(huella: str) -> None:
    _HUELLAS[huella] = 0.0

def _huella_operacion(*partes: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in partes).encode()).hexdigest()
