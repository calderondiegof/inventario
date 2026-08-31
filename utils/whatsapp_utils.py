"""Utilidades de formateo y parsing para mensajes de WhatsApp."""
from typing import Any, Dict, Iterable, List, Optional, Tuple

def construir_lista_texto_whatsapp(items: Iterable, titulo: str = "Catalogo de Materiales") -> str:
    """Crea un mensaje de TEXTO con la lista numerada de items."""
    nombres = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            nombre = item.get("nombre", "")
        else:
            nombre = str(item)
        norm = nombre.lower().strip()
        if norm and norm not in seen:
            seen.add(norm)
            nombres.append(nombre)
    nombres.sort(key=lambda n: n.lower())
    lineas = [f"\U0001F4CB *{titulo}* ({len(nombres)} disponibles):\n"]
    for idx, mat in enumerate(nombres, 1):
        lineas.append(f"{idx}. {mat}")
    lineas.append("\n_Escribe el nombre del material o el codigo para continuar._")
    return "\n".join(lineas)

def resolver_entrada_material(texto: str, nombres_ordenados: Iterable[str]) -> Optional[str]:
    """Resuelve lo que escribe el usuario tras ver la lista numerada."""
    t = (texto or "").strip()
    if not t:
        return None
    nombres = [str(n) for n in (nombres_ordenados or [])]
    if t.isdigit():
        idx = int(t)
        if 1 <= idx <= len(nombres):
            return nombres[idx - 1]
        return None
    return t

def construir_seccion_lista_interactiva(filas: Iterable, titulo_lista: str = "Materiales", boton: str = "Ver opciones") -> List[Dict[str, Any]]:
    """Construye una lista de secciones para lista interactiva de WhatsApp."""
    filas_list = list(filas)
    rows = []
    for f in filas_list:
        if isinstance(f, dict):
            rows.append({"id": f["id"], "title": f["title"], "description": f.get("description", "")})
        else:
            rows.append({"id": str(f[0]), "title": str(f[1]), "description": str(f[2]) if len(f) > 2 else ""})
    return [{"title": titulo_lista, "rows": rows}]

def parsear_edicion_precio(texto: str) -> Optional[Tuple[int, float]]:
    """Parsea el comando de edicion de precio: 'N VALOR'."""
    from utils.number_parser import _parsear_numero
    t = (texto or "").strip()
    partes = t.split()
    if len(partes) != 2:
        return None
    try:
        idx = int(partes[0])
        precio = _parsear_numero(partes[1])
        if idx < 1 or precio is None:
            return None
        return (idx, precio)
    except ValueError:
        return None

def procesar_precio_paso_a_paso(texto, items, precios, indice_esperado):
    """Maquina de estados para capturar precios uno a uno."""
    from utils.number_parser import _parsear_numero
    t = (texto or "").strip().lower()
    if t == "cancelar":
        return {"tipo": "cancelar", "precios": precios, "texto": ""}
    if t == "0" and precios:
        items_nombres = [it.get("nombre", "") for it in items]
        ultimo_nombre = next((n for n in reversed(items_nombres) if n in precios), None)
        precios = dict(precios)
        if ultimo_nombre:
            del precios[ultimo_nombre]
        idx_correccion = 1
        for i, nombre in enumerate(items_nombres, 1):
            if nombre not in precios:
                idx_correccion = i
                break
        return {
            "tipo": "corregir",
            "indice": idx_correccion - 1,
            "precios": precios,
            "texto": f"Precio descartado. Indica el precio para '{items[idx_correccion - 1].get('nombre')}' (o *0* para saltar):",
        }
    if indice_esperado < 1 or indice_esperado > len(items):
        return {"tipo": "invalido", "indice": indice_esperado, "precios": precios, "texto": ""}
    precio = _parsear_numero(t)
    if precio is None:
        return {"tipo": "invalido", "indice": indice_esperado, "precios": precios, "texto": ""}
    precios = dict(precios)
    precios[items[indice_esperado - 1].get("nombre", "")] = precio
    if indice_esperado < len(items):
        return {
            "tipo": "continuar",
            "indice": indice_esperado + 1,
            "precios": precios,
            "texto": f"Precio de '{items[indice_esperado].get('nombre')}' (o *0* para saltar):",
        }
    return {"tipo": "final", "precios": precios, "texto": ""}

def borrador_para_nueva_lista(borrador: Optional[Dict[str, Any]], modo: str = "compra") -> Dict[str, Any]:
    """Reinicia el borrador para una nueva lista de materiales."""
    resultado = {
        "bodega_id": (borrador or {}).get("bodega_id"),
        "usuario_id": (borrador or {}).get("usuario_id"),
        "intencion": modo.upper(),
        "items": [],
        "precios": {},
    }
    for clave in ("cliente", "conductor", "conductor_id", "fecha"):
        if clave in (borrador or {}):
            resultado[clave] = (borrador or {})[clave]
    return resultado

def es_lista_materiales(texto: str) -> bool:
    """Detecta si el texto parece una lista de materiales con cantidades."""
    import re
    if not texto:
        return False
    patron = re.compile(r"^\s*.+?[\s\-:]+(\d+(?:[.,]\d+)?)\s*(?:kg)?\s*$", re.IGNORECASE | re.MULTILINE)
    return bool(patron.search(texto))

def formatear_resumen_precios(items: Iterable[Dict[str, Any]], precios: Dict[str, float]) -> str:
    """Genera el resumen final de precios con instrucciones de confirmacion."""
    lineas = ["\u2705 *Revision de precios*:\n"]
    for idx, item in enumerate(items, 1):
        nombre = item.get("nombre", "???")
        precio = precios.get(nombre, 0.0)
        lineas.append(f"{idx}. {nombre:15s} -> {precio:,.2f} /kg")
    lineas.append("\n*OK* o *SI* para confirmar.")
    lineas.append("*N VALOR* para editar (ej: *2 16700*).")
    lineas.append("*0* o *CANCELAR* para descartar todo.")
    return "\n".join(lineas)
