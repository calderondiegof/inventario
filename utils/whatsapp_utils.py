"""Utilidades de formateo y parsing para mensajes de WhatsApp."""
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _nombre_material(item: Any) -> str:
    """Extrae el nombre visible de un item de material de forma defensiva.

    Acepta mÃºltiples esquemas de clave (``material`` / ``nombre`` /
    ``material_nombre``) para tolerar las dos formas en que el flujo
    almacena los items en contexto:
      - ``procesar_precio_paso_a_paso`` (remisiÃ³n): ``material_nombre``.
      - Wizard de compra/entrada genÃ©rico: ``nombre`` o ``material``.

    Devuelve el nombre encontrado, o ``"el material"`` como fallback
    cuando el item es None o no contiene ninguna clave reconocible.
    """
    if not isinstance(item, dict):
        return "el material"
    for clave in ("material", "nombre", "material_nombre"):
        valor = item.get(clave)
        if valor:
            return str(valor)
    mid = item.get("movimiento_id")
    return str(mid) if mid else "el material"

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
            rows.append({"id": f["id"], "title": f["title"][:24], "description": f.get("description", "")[:72]})
        else:
            rows.append({"id": str(f[0]), "title": str(f[1])[:24], "description": str(f[2])[:72] if len(f) > 2 else ""})
    return [{"title": titulo_lista[:24], "rows": rows}]

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
    """Maquina de estados para capturar precios uno a uno.

    ``indice_esperado`` es 1-indexado: el valor 1 representa el primer
    material de la lista. ``precios`` se indexa por el nombre visible del
    material (``_nombre_material``) para que ``formatear_resumen_precios``
    pueda resolverlo sin conocer el esquema interno de cada flujo.
    """
    from utils.number_parser import _parsear_numero
    t = (texto or "").strip().lower()
    if t == "cancelar":
        return {"tipo": "cancelar", "precios": precios, "texto": ""}
    if t == "0" and precios:
        mid_a_nombre = {
            str(it.get("movimiento_id", "")): _nombre_material(it)
            for it in items
        }
        ultimo_nombre = None
        for k in reversed(list(precios.keys())):
            if k in mid_a_nombre:
                ultimo_nombre = mid_a_nombre[k]
                break
            elif k in {_nombre_material(it) for it in items}:
                ultimo_nombre = k
                break
        precios = dict(precios)
        if ultimo_nombre:
            borrado = False
            for k, nm in mid_a_nombre.items():
                if nm == ultimo_nombre and k in precios:
                    del precios[k]
                    borrado = True
                    break
            if not borrado and ultimo_nombre in precios:
                del precios[ultimo_nombre]
        idx_correccion = 1
        for i, it in enumerate(items, 1):
            nm = _nombre_material(it)
            mid = it.get("movimiento_id")
            clave = str(mid) if mid is not None else nm
            if clave not in precios and nm not in precios:
                idx_correccion = i
                break
        return {
            "tipo": "corregir",
            "indice": idx_correccion,
            "precios": precios,
            "texto": (
                f"Precio descartado. Indica el precio para "
                f"'{_nombre_material(items[idx_correccion - 1])}' "
                f"(o *0* para saltar):"
            ),
        }
    if indice_esperado < 1 or indice_esperado > len(items):
        nombre = (
            _nombre_material(items[indice_esperado - 1])
            if 1 <= indice_esperado <= len(items)
            else "el material"
        )
        return {
            "tipo": "invalido",
            "indice": indice_esperado,
            "precios": precios,
            "texto": (
                f"No pude identificar el material. Indica el precio por kilo "
                f"para '{nombre}' (o *0* para saltar):"
            ),
        }
    precio = _parsear_numero(t)
    if precio is None:
        nombre = _nombre_material(items[indice_esperado - 1])
        return {
            "tipo": "invalido",
            "indice": indice_esperado,
            "precios": precios,
            "texto": (
                f"âš ï¸ '{texto}' no es un precio vÃ¡lido. Indica el valor "
                f"numÃ©rico por kilo para '{nombre}' (ej. 16000) o *0* para saltar:"
            ),
        }
    item_actual = items[indice_esperado - 1]
    nombre_actual = _nombre_material(item_actual)
    precios = dict(precios)
    precios[nombre_actual] = precio
    if indice_esperado < len(items):
        return {
            "tipo": "continuar",
            "indice": indice_esperado + 1,
            "precios": precios,
            "texto": (
                f"Precio de '{_nombre_material(items[indice_esperado - 1])}' "
                f"(o *0* para saltar):"
            ),
        }
    return {"tipo": "final", "precios": precios, "texto": "", "indice": indice_esperado}
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
    """Genera el resumen final de precios con instrucciones de confirmacion.

    El ``precios`` puede estar indexado por ``movimiento_id`` (caso real de
    remisiones en producciÃ³n) o por nombre visible (compatibilidad con
    tests y wizards genÃ©ricos). Se intenta siempre con la clave del item
    actual antes de imprimir ``0.00``.
    """
    lineas = ["\u2705 *Revision de precios*:\n"]
    for idx, item in enumerate(items, 1):
        nombre = _nombre_material(item)
        # Buscar primero por movimiento_id y luego por nombre para tolerar
        # ambos esquemas de almacenamiento.
        mid = item.get("movimiento_id")
        precio = 0.0
        if mid is not None and str(mid) in precios:
            precio = precios[str(mid)]
        elif nombre in precios:
            precio = precios[nombre]
        lineas.append(f"{idx}. {nombre:15s} -> {precio:,.2f} /kg")
    lineas.append("\n*OK* o *SI* para confirmar.")
    lineas.append("*N VALOR* para editar (ej: *2 16700*).")
    lineas.append("*0* o *CANCELAR* para descartar todo.")
    return "\n".join(lineas)
