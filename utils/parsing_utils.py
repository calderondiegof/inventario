"""Parsing de bloques de texto libre.

- Bloques de persona (nombre, documento, telefono, direccion, placa)
- Sinonimos y frases compuestas para normalizacion
- Deduplicacion de operaciones (huellas)
"""
import re
import hashlib
from typing import Any, Dict, List, Optional, Tuple

_PATRON_DOCUMENTO = re.compile(
    r"\b(CC|C\.C\.|DNI|NIT|CUIT|NID|ID|CEDULA|CED)\s*[:\-]?\s*(\d[\d\s.\-]{4,})\b", re.IGNORECASE)
# Teléfono: etiqueta + número (conserva el '+' inicial) o secuencia suelta de
# 10+ dígitos. El prefijo se separa DESPUÉS para no perder el '+'.
_PATRON_CELULAR = re.compile(
    r"(?:\b(?:cel|celular|tel|telefono|teléfono|movil|móvil|whatsapp|wsp)\s*[:\-]?\s*(\+?\d[\d\s.\-]{7,})\b)"
    r"|(?<!\d)(\+?\d[\d\s.\-]{9,})(?!\d)",
    re.IGNORECASE)
# Placas/patentes flexibles (Colombia AAA123 / Argentina AA123BB).
_PATRON_PLACA = re.compile(
    r"\b(?:[A-Za-z]{3}[\-]?\d{2,3}[\-]?[A-Za-z]{0,2}"
    r"|(?:[A-Za-z]{2}\d{2,3}[A-Za-z]{2}))\b",
    re.IGNORECASE)
# Dirección: cualquier línea que CONTENGA una nomenclatura vial (la línea
# completa es la dirección: 'Calle 10 #5-20, Villa Constitución, Argentina').
_NOMENCLATURAS = (
    r"\b(?:Calle|Cll|Clle|Cl|Carrera|Cra|Cr|Av|Avenida|Diagonal|Diag|Dg|"
    r"Transversal|Tv|Tvz|Autopista|Pasaje|Pje|Via|Vía)\b")
_PATRON_DIRECCION = re.compile(
    rf"([^\n]*(?:{_NOMENCLATURAS})[^\n]*)", re.IGNORECASE)
# DNI/Cédula numérica suelta (6-8 dígitos) sin etiqueta.
_PATRON_DNI_SUELTO = re.compile(r"\b(\d{6,8})\b")
_PAISES = re.compile(
    r"\b(argentina|colombia|chile|peru|perú|uruguay|mexico|méxico|brasil|"
    r"bolivia|ecuador|paraguay|venezuela)\b", re.IGNORECASE)

def _normalizar_lineas(texto: str) -> List[str]:
    return [ln.strip() for ln in (texto or "").split("\n") if ln.strip()]

def _limpiar_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")

def normalizar_digitos(texto: str) -> str:
    """Extrae solo los dígitos de un texto (identificaciones)."""
    return _limpiar_digitos(texto)

def _tiene_nomenclatura(linea: str) -> bool:
    return bool(_PATRON_DIRECCION.search(linea))

def extraer_placas(texto: str) -> Tuple[Optional[str], Optional[str]]:
    """Extrae hasta DOS placas/patentes de un texto:
    - 1ra coincidencia -> placa del camión/tractora (placa_conductor).
    - 2da coincidencia -> placa del remolque (placa_trailer_conductor).
    Soporta 'AAA123 / BBB456', 'Placa: AAA123, Trailer: BBB456' o patentes
    argentinas ('patente AA123BB / remolque CC456DD')."""
    encontradas = [m.group(0).upper().replace(" ", "")
                   for m in _PATRON_PLACA.finditer(texto or "")]
    vistas: List[str] = []
    for pl in encontradas:
        if pl not in vistas:
            vistas.append(pl)
    return (vistas[0] if vistas else None,
            vistas[1] if len(vistas) > 1 else None)

def parsear_bloque_persona(texto: str) -> Dict[str, str]:
    """Extrae nombre, identificacion, telefono, direccion y placas de un
    mensaje en bloque (formatos Colombia y Argentina):
    - telefono conserva el '+' inicial ('Cel: +54 9 11 1234-5678' ->
      '+5491112345678').
    - direccion es la LÍNEA COMPLETA con nomenclatura vial; si la ciudad/país
      vienen en la línea siguiente (con ',' o país), se CONCATENAN."""
    lineas = _normalizar_lineas(texto)
    resultado: Dict[str, Any] = {
        "nombre": None, "identificacion": None, "telefono": None,
        "direccion": None, "placa": None, "placa_trailer": None,
    }
    candidatos_nombre: List[str] = []
    for linea in lineas:
        ln = linea.strip().lstrip("*•-").strip()
        if not ln:
            continue
        ident_m = _PATRON_DOCUMENTO.search(ln)
        tel_m = _PATRON_CELULAR.search(ln)
        # Placas: la línea puede traer las dos ('AAA123, Trailer: BBB456').
        p1, p2 = extraer_placas(ln)
        if p1:
            if not resultado["placa"]:
                resultado["placa"] = p1
                if p2:
                    resultado["placa_trailer"] = p2
            elif not resultado["placa_trailer"] and p1 != resultado["placa"]:
                resultado["placa_trailer"] = p1
        if ident_m:
            resultado["identificacion"] = _limpiar_digitos(ident_m.group(2))
        if tel_m:
            telefono = (tel_m.group(1) or tel_m.group(2)).strip()
            telefono = re.sub(r"[\s.\-]", "", telefono)
            resultado["telefono"] = telefono
        # Dirección: línea completa con nomenclatura vial.
        if _PATRON_DIRECCION.search(ln) and not (ident_m or tel_m):
            resultado["direccion"] = ln
            continue
        # Continuación de dirección (ciudad, provincia o país en la línea
        # siguiente, sin datos técnicos) -> se concatena.
        if resultado["direccion"] and not (ident_m or tel_m) and not p1 and \
                ("," in ln or _PAISES.search(ln)):
            resultado["direccion"] = f"{resultado['direccion']}, {ln}"
            continue
        # Línea sin datos técnicos -> candidata a nombre.
        if not (ident_m or tel_m):
            candidatos_nombre.append(ln)
    # Nombre: primera línea limpia (sin nomenclatura vial).
    for cand in candidatos_nombre:
        if _tiene_nomenclatura(cand):
            continue
        resultado["nombre"] = cand
        break
    # Cédula/DNI numérico suelto (6-8 dígitos) sin etiqueta.
    if not resultado["identificacion"]:
        for cand in candidatos_nombre:
            dni = _PATRON_DNI_SUELTO.search(cand)
            if dni:
                resultado["identificacion"] = dni.group(0)
                resultado["nombre"] = (resultado["nombre"] or
                                       cand.replace(dni.group(0), "")).strip()
                break
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
