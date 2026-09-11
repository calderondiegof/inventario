"""Fast path determinista para mensajes estructurados.

Los operadores escriben la mayoría de registros con bloques fijos de
WhatsApp (encabezado + viñetas ``Material Cantidad`` + fecha opcional):

    08-09
    Cooperativa 3135

    Selección
    * Grueso 1084
    * Lamina 329
    * Basura 1010

Para esos mensajes NO hace falta llamar a DeepSeek: el parseo es
determinista (más rápido, más barato y sin riesgo de alucinaciones).
``intentar_fast_path`` devuelve un borrador con la MISMA forma que produce
la IA (``intencion``, ``items``, ``merma_kg``, ``entradas_revuelto``,
``fecha_operacion``) solo cuando el mensaje encaja 100% con el formato; si
cualquier línea se le escapa devuelve ``None`` y el handler cae al flujo de
IA (fallback conversacional). Así la IA se mantiene "para conversar e
interpretar" y el código "calcula y registra".
"""
import logging
import re
from typing import Any, Dict, List, Optional

from utils.parsers import (
    es_nombre_merma, extraer_fecha_texto, parsear_fecha_colombiana,
    parsear_material_cantidad,
)
from utils.text_normalizer import normalizar

logger = logging.getLogger(__name__)

# Encabezados de bloque → intención. Se aceptan con o sin tilde.
_INTENCIONES_ENCABEZADO = {
    "seleccion": "SELECCION_REVUELTO",
    "seleccion de revuelto": "SELECCION_REVUELTO",
    "seleccion revuelto": "SELECCION_REVUELTO",
    "material seleccionado": "SELECCION_REVUELTO",
    "materiales seleccionados": "SELECCION_REVUELTO",
    "venta": "VENTA_DESPACHO",
    "salida": "VENTA_DESPACHO",
    "despacho": "VENTA_DESPACHO",
    "orden de salida": "VENTA_DESPACHO",
    "entrada": "AJUSTE_INVENTARIO",
    "ingreso": "AJUSTE_INVENTARIO",
    "registro diario": "REGISTRO_DIARIO",
}

# Líneas de bloque secundario sin intención que se ignoran (no invalidan el
# fast path). NO incluyen los encabezados con intención (Selección/Venta/
# Entrada/Registro diario...): esos los consume _INTENCIONES_ENCABEZADO para
# fijar la intención del borrador.
_LINEAS_RUIDO = {
    "materiales", "material", "registro", "compras", "compra", "revuelto",
    "registro de revuelto",
}

# ``Nombre con espacios 123.4 kg`` (la misma forma que las viñetas).
_RE_FUENTE_KG = re.compile(
    r"^\s*(.+?)[\s\-:]+(\d+(?:[.,]\d+)?)\s*(?:kg)?\s*$", re.IGNORECASE)

_MERMA_KEYS = {"merma", "merma kg", "merma_kg", "basura kg"}


def _limpiar_linea(linea: str) -> str:
    ln = (linea or "").strip()
    # Viñetas de WhatsApp y numeraciones.
    ln = re.sub(r"^[\*\-\•\u2022\d]+[\)\.\:\s]+", "", ln).strip()
    return ln


def _fusionar_borrador(anterior: Dict[str, Any], datos: Dict[str, Any]) -> Dict[str, Any]:
    """Fusiona el borrador del fast path con el pendiente del contexto, con la
    misma semántica de ``fusionar_borrador`` de la IA: los items y las entradas
    se ACUMULAN por nombre/fuente (no se sobrescriben) y los escalares solo se
    sobrescriben cuando el nuevo mensaje los trae."""
    resultado = dict(anterior or {})
    if datos.get("items"):
        existentes = {i["material_nombre"].lower(): dict(i) for i in resultado.get("items", [])}
        for item in datos["items"]:
            key = item["material_nombre"].lower()
            if key in existentes:
                existentes[key]["cantidad_kg"] = float(existentes[key].get("cantidad_kg") or 0) + float(item["cantidad_kg"])
            else:
                existentes[key] = dict(item)
        resultado["items"] = list(existentes.values())
    if datos.get("entradas_revuelto"):
        existentes = {e["fuente_nombre"].lower(): dict(e) for e in resultado.get("entradas_revuelto", [])}
        for entrada in datos["entradas_revuelto"]:
            key = entrada["fuente_nombre"].lower()
            if key in existentes:
                existentes[key]["cantidad_kg"] = float(existentes[key].get("cantidad_kg") or 0) + float(entrada.get("cantidad_kg") or 0)
            else:
                existentes[key] = dict(entrada)
        resultado["entradas_revuelto"] = list(existentes.values())
    for clave, valor in datos.items():
        if clave in ("items", "entradas_revuelto") or valor in ([], "", None):
            continue
        resultado[clave] = valor
    return resultado


def intentar_fast_path(texto: str, inventario, borrador: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Intenta interpretar el mensaje SIN IA. Devuelve el borrador fusionado
    (dict con la misma forma que produce ``inferir_datos_ia``) o ``None`` si
    el mensaje no encaja con el formato estructurado (y debe ir a la IA).

    Regla conservadora: cualquier línea que no se pueda clasificar de forma
    determinista invalida el fast path y devuelve ``None``. Prefiero llamar a
    la IA antes que adivinar.
    """
    lineas = [_limpiar_linea(l) for l in (texto or "").split("\n")]
    lineas = [l for l in lineas if l]
    if not lineas:
        return None

    items: List[Dict[str, Any]] = []
    entradas: List[Dict[str, Any]] = []
    merma_kg = 0.0
    intencion: Optional[str] = None
    fecha: Optional[str] = None
    n_clasificadas = 0

    for ln in lineas:
        ln_norm = normalizar(ln)
        # 1) Encabezados de bloque: fijan la intención si aún no hay una.
        intencion_hdr = _INTENCIONES_ENCABEZADO.get(ln_norm)
        if intencion_hdr:
            if intencion is None:
                intencion = intencion_hdr
            n_clasificadas += 1
            continue
        # 2) Líneas de bloque secundario sin intención (ruido).
        if ln_norm in _LINEAS_RUIDO:
            n_clasificadas += 1
            continue
        # 3) Líneas de fecha puras (08-09, 04-09-2026, 04 sep, ayer...).
        f = parsear_fecha_colombiana(ln_norm)
        if f:
            if fecha is None:
                fecha = f
            n_clasificadas += 1
            continue
        # 4) Líneas "Material Cantidad" / "Fuente Cantidad".
        par = parsear_material_cantidad(ln)
        if not par:
            # Línea libre no clasificable → el mensaje no es estructurado.
            logger.info("Fast path: línea no clasificable '%s' → fallback IA", ln)
            return None
        nombre, cantidad = par
        # Guard de seguridad: si el nombre parseado contiene dígitos, la línea
        # tenía DOS números (ej. 'Grueso 7117 -2500' = peso -precio). El
        # genérico se comería el peso y trataría el precio como cantidad.
        # No se interpreta: se aborta y el mensaje cae al flujo de IA.
        if any(ch.isdigit() for ch in nombre):
            logger.info("Fast path: línea con dos números '%s' → fallback IA", ln)
            return None
        n_clasificadas += 1
        # 4a) ¿Es una fuente de origen (entrada de Revuelto)?
        fuente = inventario.obtener_fuente_por_nombre(nombre) if inventario else None
        if fuente:
            entradas.append({"fuente_nombre": fuente.nombre, "cantidad_kg": cantidad})
            continue
        # 4b) ¿Es merma? Por nombre (basura*/tierra*, 'Merma N') o por tipo
        # MERMA del catálogo. Nunca al inventario.
        nombre_norm = normalizar(nombre)
        if es_nombre_merma(nombre) or nombre_norm in _MERMA_KEYS:
            merma_kg += cantidad
            continue
        # 4c) Material del catálogo (incluye sinónimos: grueso→Carter...).
        mat = inventario.obtener_material_por_nombre(nombre) if inventario else None
        items.append({
            "material_nombre": mat.nombre if mat else nombre,
            "cantidad_kg": cantidad,
        })

    # Conservador: todas las líneas debieron clasificarse y debe haber
    # contenido interpretable (materiales, entradas o merma).
    if n_clasificadas != len(lineas) or not (items or entradas or merma_kg):
        return None

    # Una sola fuente "Cooperativa 3135" sin selección es ambigua (podría ser
    # un cliente/compra): ese caso conversacional se deja a la IA.
    if entradas and not items and not merma_kg and len(lineas) <= 2:
        return None

    datos: Dict[str, Any] = {}
    if intencion:
        datos["intencion"] = intencion
    if entradas:
        datos["intencion"] = "REGISTRO_DIARIO"
    if items:
        datos["items"] = items
    if merma_kg:
        datos["merma_kg"] = merma_kg
    if entradas:
        datos["entradas_revuelto"] = entradas
    # Fecha determinista (inline o dentro de cualquier línea).
    if not fecha:
        fecha = extraer_fecha_texto(texto)
    if fecha:
        datos["fecha_operacion"] = fecha

    return _fusionar_borrador(borrador or {}, datos)

