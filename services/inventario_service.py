import re
import time
import hashlib
import unicodedata
import uuid
import difflib
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from supabase import Client


class TipoMaterial(str, Enum):
    BRUTO = "BRUTO"
    SEMILIMPIO = "SEMILIMPIO"
    LIMPIO = "LIMPIO"
    MERMA = "MERMA"
    # Alias heredado de la versión anterior; al cargar el catálogo se
    # normaliza a MERMA. Se mantiene por compatibilidad con la BD existente.
    DESPERDICIO = "DESPERDICIO"


class TipoTransaccion(str, Enum):
    COMPRA = "COMPRA"
    ENTRADA_BRUTA = "ENTRADA_BRUTA"
    VENTA = "VENTA"
    TRANSFORMACION = "TRANSFORMACION"
    DESPACHO = "DESPACHO"
    AJUSTE_INVENTARIO = "INVENTARIO_INICIAL"
    ANULACION = "ANULACION"


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", (texto or "").strip().lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto)


def construir_lista_texto_whatsapp(items: Iterable[str],
                                   titulo: str = "Catálogo de Materiales") -> str:
    """Crea un mensaje de TEXTO con la lista numerada de ítems (>10 de forma típica).

    La API de WhatsApp Cloud limita los Interactive List Messages a 10 filas
    TOTALES (error #131009). Para catálogos mayores (30+ materiales) se envía
    texto plano ordenado alfabéticamente, evitando el error 400.

    Cada línea se numera correlativamente con enumerate(..., 1) para que el
    usuario pueda responder "6" y el sistema resuelva la posición 6 (ver
    resolver_entrada_material).

    Formato:
      📋 *{titulo}* ({n} disponibles):

      1. {material}
      2. {material}
      ...

      _Escribe el nombre del material o el código para continuar._
    """
    nombres = sorted({str(i) for i in items})  # alfabético y sin duplicados
    lineas = [f"📋 *{titulo}* ({len(nombres)} disponibles):\n"]
    for idx, mat in enumerate(nombres, 1):
        nombre = mat.get("nombre") if isinstance(mat, dict) else mat
        lineas.append(f"{idx}. {nombre}")
    lineas.append("\n_Escribe el nombre del material o el código para continuar._")
    return "\n".join(lineas)


def resolver_entrada_material(texto: str,
                              nombres_ordenados: Iterable[str]) -> Optional[str]:
    """Dado lo que escribe el usuario tras ver la lista numerada, devuelve el
    nombre del material a consultar:
      - Si es un número '6' -> nombres_ordenados[5] (posición 6 de la lista
        que se mostró, que está ordenada alfabéticamente).
      - Si es un nombre -> se devuelve tal cual (se resuelve por coincidencia).
    Devuelve None si el índice está fuera de rango."""
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


def _parsear_numero_moneda(texto: str) -> Optional[float]:
    """Parsea un número escrito con formato español o inglés (misma semántica
    que main._parsear_numero): '4120,50' -> 4120.5 | '1.250.000' -> 1250000 |
    '1,250.50' -> 1250.5. Devuelve None si no es válido."""
    t = (texto or "").strip().replace(" ", "").replace("$", "")
    if not t:
        return None
    if "," in t and "." in t:
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


# -----------------------------------------------------------------------------
# Parser de bloques para el módulo unificado de creación (Cliente / Conductor).
# Procesa un mensaje de varias líneas o texto continuo y extrae los campos por
# patrones flexibles (nomenclaturas de Colombia y Argentina).
# -----------------------------------------------------------------------------
_PATRON_IDENTIFICACION = re.compile(
    r"\b(CC|NIT|DNI|CUIT|NID|ID|CEDULA|CEDULA DE CIUDADANIA|CED)\s*[:\-]?\s*(\d[\d\s.\-]{4,})\b",
    re.IGNORECASE,
)
_PATRON_TELEFONO = re.compile(
    r"(?:\b(?:cel|celular|tel|telefono|tel\b|movil|móvil)\s*[:\-]?\s*(\+?\d[\d\s.\-]{7,})\b)"
    r"|(?<!\d)(?:\+?\d[\d\s.\-]{9,})(?!\d)",
    re.IGNORECASE,
)
_NOMENCLATURAS = r"\b(?:Calle|Cl|Cra|Carrera|Cr|Av|Avenida|Diagonal|Dg|Transversal|Tv|Autopista|Pasaje|Pje|Cll|Clle|Diag|Tvz|Via|Vía)\b"
_PATRON_DIRECCION = re.compile(
    rf"([^\n]*(?:{_NOMENCLATURAS})[^\n]*)", re.IGNORECASE,
)
_PATRON_PLACA = re.compile(
    r"\b(?:[A-Za-z]{3}[\-]?\d{2,3}[\-]?[A-Za-z]{0,2})"
    r"|(?:[A-Za-z]{2}\d{2,3}[A-Za-z]{2})\b",
    re.IGNORECASE,
)
_PATRON_FECHA_DNI = re.compile(r"\b(\d{6,8})\b")  # DNI numérico sin etiqueta


def _limpiar_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


def normalizar_digitos(texto: str) -> str:
    """Extrae solo los dígitos de un texto (quita separadores, guiones, prefijos
    de país en teléfonos, letras de placas no aplican aquí). Usado para
    identificaciones y teléfonos en el paso a paso de creación."""
    return _limpiar_digitos(texto)


def parsear_bloque_persona(texto: str) -> Dict[str, str]:
    """Extrae nombre, identificacion, telefono, direccion y placa (opcional)
    de un mensaje en bloque para Cliente o Conductor.

    - Nombre: primera línea no vacía (quitando prefijos de otros campos).
    - identificacion: etiqueta CC/NIT/DNI/CUIT/ID/NID + números, o un DNI de 6-8
      dígitos suelto; se ESTRIPA el prefijo y se dejan solo los dígitos.
    - telefono: etiqueta cel/celular/tel/telefono + números, o secuencia de 10+
      dígitos (se conserva el '+ ' si existe).
    - direccion: líneas que contienen nomenclatura de Calle/Cra/Av/Dg/Tv/...
    - placa: patrón de placa vehicular (solo relevante para Conductor).
    """
    lineas = [l.strip().lstrip("*•-").strip() for l in _normalizar_lineas(texto)]
    nombre_candidatos: List[str] = []
    identificacion = telefono = direccion = placa = ""

    for linea in lineas:
        placa_m = _PATRON_PLACA.search(linea)
        if placa_m:
            placa = placa_m.group(0).strip().upper().replace(" ", "")
        ident_m = _PATRON_IDENTIFICACION.search(linea)
        if ident_m:
            identificacion = _limpiar_digitos(ident_m.group(2))
        tel_m = _PATRON_TELEFONO.search(linea)
        if tel_m:
            telefono = tel_m.group(0).strip().lstrip("cel: celular: tel: telefono: movil: mobile: ").strip()
            telefono = re.sub(r"[\s.\-]", "", telefono)
            for pref in ("cel:", "celular:", "tel:", "telefono:", "movil:", "móvil:", "cel", "celular", "tel", "telefono", "movil", "móvil"):
                if telefono.lower().startswith(pref):
                    telefono = telefono[len(pref):].strip()
                    break
        if _PATRON_DIRECCION.search(linea):
            direccion = linea
            continue
        # Si la línea no es identificable como campo técnico, puede ser nombre.
        if not (ident_m or tel_m or placa_m):
            nombre_candidatos.append(linea)

    # Nombre: primera línea "limpia" (línea principal del bloque).
    nombre = ""
    for cand in nombre_candidatos:
        if _tiene_nomenclatura(cand):
            continue
        nombre = cand
        break

    # Si no se reconoció identificación por etiqueta y no aparece ya en otra
    # línea, intentamos un DNI numérico suelto.
    if not identificacion:
        for cand in nombre_candidatos:
            dni = _PATRON_FECHA_DNI.search(cand)
            if dni and len(_limpiar_digitos(dni.group(0))) == len(dni.group(0)) \
               and len(_limpiar_digitos(dni.group(0))) >= 6:
                identificacion = dni.group(0)
                nombre = nombre.replace(dni.group(0), "").strip()
                break

    return {
        "nombre": nombre.strip(),
        "identificacion": identificacion,
        "telefono": telefono,
        "direccion": direccion,
        "placa": placa,
    }


def _normalizar_lineas(texto: str) -> List[str]:
    return [l for l in re.split(r"[\n;]+", texto or "") if l.strip()]


def _tiene_nomenclatura(texto: str) -> bool:
    return bool(_PATRON_DIRECCION.search(texto))


def formatear_resumen_precios(items: Iterable[Dict[str, Any]],
                              precios: Dict[str, float]) -> str:
    """Construye el resumen FINAL enumerado (1..N) del flujo de precios de una
    Remisión, con instrucciones ultra claras para quien lo recibe por WhatsApp.

    - 'OK' o 'SI' procesa la orden.
    - '[número] [precio]' corrige un ítem (ej. '2 16700').
    - '0' o 'CANCELAR' anula la operación.
    """
    lineas = ["📋 *Resumen de precios por kilo:*", ""]
    for i, it in enumerate(items, 1):
        precio = (precios or {}).get(str(it["movimiento_id"]))
        precio_txt = f"{precio:,.2f}" if precio is not None else "—"
        lineas.append(f"{i}. {it['material_nombre']} ({it['cantidad_kg']:,.2f} kg): {precio_txt} /kg")
    lineas += [
        "",
        "_Instrucciones:_",
        "• Escribe *OK* o *SI* para procesar la orden.",
        "• Para corregir un ítem: *[número] [nuevo_precio]* (ej. *2 16700*).",
        "• Escribe *0* o *CANCELAR* para anular la operación.",
    ]
    return "\n".join(lineas)


def parsear_edicion_precio(texto: str) -> Optional[Tuple[int, float]]:
    """Parsea la corrección de un ítem del resumen: '[número] [nuevo_precio]'
    (ej. '2 16700'). Devuelve (indice_1, precio_float) o None si no coincide."""
    m = re.fullmatch(r"\s*(\d+)\s+([\d.,]+)\s*", (texto or "").strip())
    if not m:
        return None
    idx = int(m.group(1))
    precio = _parsear_numero_moneda(m.group(2))
    if idx < 1 or precio is None or precio <= 0:
        return None
    return (idx, precio)


def procesar_precio_paso_a_paso(texto: str, items: Iterable[Dict[str, Any]],
                                precios: Dict[str, float],
                                indice: int) -> Dict[str, Any]:
    """Procesa la respuesta del usuario en el bucle de captura de precios por
    kilo, ítem por ítem. Devuelve un dict con la nueva fotografía del estado:

      {'tipo': 'ok'|'corregir'|'invalido',
       'precios': dict actualizado,
       'indice': int nuevo,
       'texto': str mensaje del bot}

    Reglas:
      - 'ok': se registró el precio del ítem `indice`; precios con la nueva
        clave; `indice` avanza +1.
      - 'corregir': el usuario escribió '0' -> se DESCARTA el precio del
        material anterior (`indice-1`, ya registrado) y se vuelve a solicitar
        (indice -1). Corrige ágilmente el paso a paso.
      - 'invalido': precio no válido (o '0' sin ítem anterior): sin cambios.
    """
    items = list(items)
    precio = _parsear_numero_moneda(texto)
    precios = dict(precios or {})

    # '0' con un ítem anterior -> descartar el anterior y repetirlo.
    if precio == 0 and indice >= 1 and indice <= len(items):
        anterior = items[indice - 1]
        precios.pop(str(anterior["movimiento_id"]), None)
        return {
            "tipo": "corregir", "precios": precios, "indice": indice - 1,
            "texto": (f"✖ Se descartó el precio de {anterior['material_nombre']}. "
                      f"Ingrese nuevamente su precio por kilo (en moneda local) "
                      f"({anterior['cantidad_kg']:,.2f} kg):"),
        }

    if precio is None or precio < 0 or precio == 0:
        return {
            "tipo": "invalido", "precios": precios, "indice": indice,
            "texto": ("Precio inválido. Ingrese el precio por kilo en moneda local "
                      "(ejemplo: 3500), escriba '0' para corregir el anterior "
                      "o *cancelar*."),
        }

    actual = items[indice]
    precios[str(actual["movimiento_id"])] = precio
    return {
        "tipo": "ok", "precios": precios, "indice": indice + 1,
        "texto": f"Precio registrado: {precio:,.2f}/kg.",
    }


def construir_seccion_lista_interactiva(filas: Iterable,
                                        titulo_lista: str = "Materiales") -> List[Dict[str, Any]]:
    """Devuelve las sections de un Interactive List Message de WhatsApp.

    La API limita el TOTAL de filas a 10 (error #131009), por lo que esta
    función se usa SOLO cuando hay <=10 elementos; si hay más, main.py debe
    optar por texto (construir_lista_texto_whatsapp). Cada fila de entrada es
    (id, titulo, [descripcion])."""
    rows = []
    for fila in filas:
        id_, titulo = fila[0], fila[1]
        row: Dict[str, Any] = {"id": str(id_)[:200], "title": str(titulo)[:24]}
        if len(fila) > 2 and fila[2]:
            row["description"] = str(fila[2])[:72]
        rows.append(row)
    return [{"title": str(titulo_lista)[:24], "rows": rows}]


# Palabras que el negocio usa como sinónimos/homónimos de un material del
# catálogo. Se sustituyen palabra por palabra, sobre el texto ya normalizado
# (sin tildes, en minúsculas), ANTES de buscar el material en el catálogo.
SINONIMOS_MATERIAL: Dict[str, str] = {
    "grueso": "carter",
    "rechazo": "arreglo",
}


def aplicar_sinonimos(texto_normalizado: str) -> str:
    """Sustituye homónimos de negocio (ej. 'grueso' -> 'carter',
    'rechazo' -> 'arreglo') dentro de un texto ya normalizado."""
    palabras = texto_normalizado.split(" ")
    return " ".join(SINONIMOS_MATERIAL.get(p, p) for p in palabras)


# Frases completas (no una sola palabra) que el negocio maneja como un único
# material del catálogo. Se compara contra el texto normalizado ya completo,
# después de aplicar sinónimos de palabra suelta.
FRASES_MATERIAL: Dict[str, str] = {
    "arreglo cobre": "arreglo cobre y bronce",
    "arreglo de cobre": "arreglo cobre y bronce",
    "arreglo bronce": "arreglo cobre y bronce",
    "arreglo de bronce": "arreglo cobre y bronce",
}

def normalizar_nombre_material(texto: str) -> str:
    if not texto:
        return ""
    
    texto_limpio = texto.strip().lower()
    
    # Mapeo de sinónimos / correcciones automáticas
    sinonimos = {
        "grueso": "carter",
        "rechazo": "arreglo"
    }
    
    # Si la palabra está en el diccionario, la reemplaza
    if texto_limpio in sinonimos:
        return sinonimos[texto_limpio].upper()
        
    return texto.upper()

def aplicar_frases(texto_normalizado: str) -> str:
    """Unifica frases completas que el negocio trata como un solo material
    (ej. 'arreglo cobre' o 'arreglo bronce' -> 'arreglo cobre y bronce')."""
    return FRASES_MATERIAL.get(texto_normalizado, texto_normalizado)


# Línea "Material Cantidad" (opcional viñeta y unidad 'kg'), usada por el
# resolutor determinista de listas y la detección de reintentos.
_LINEA_MATERIAL_CANTIDAD = re.compile(
    r"^\s*(.+?)[\s\-:]+(\d+(?:[.,]\d+)?)\s*(?:kg)?\s*$", re.IGNORECASE
)


def es_lista_materiales(texto: str) -> bool:
    """True si el mensaje del usuario es (esencialmente) una lista de
    selección de materiales: la mayoría de sus líneas son 'Material
    Cantidad' (con o sin viñeta * - •)."""
    lineas = [l.strip().lstrip("*-•").strip()
              for l in re.split(r"[\n,;]+", texto or "") if l.strip()]
    if not lineas:
        return False
    pares = [l for l in lineas if _LINEA_MATERIAL_CANTIDAD.match(l)]
    return len(pares) >= max(1, len(lineas) - 1)


def borrador_para_nueva_lista(borrador: Optional[Dict[str, Any]],
                              texto: str) -> Dict[str, Any]:
    """Prepara el borrador para fusionar la extracción de un NUEVO mensaje.

    Si el mensaje es una lista de materiales (reintento del usuario), la lista
    anterior del borrador se SOBRESCRIBE (items = []) para que la fusión no
    CONCATENE los ítems del intento fallido con los del nuevo. El resto de
    campos del borrador (intención, cliente, fecha…) se conserva. Si el
    mensaje no es una lista, el borrador pasa intacto."""
    borrador_limpio = dict(borrador or {})
    if es_lista_materiales(texto):
        borrador_limpio["items"] = []
    return borrador_limpio


# --- Idempotencia de operaciones (Protección 2 contra duplicados) -----------
# Si la MISMA operación (misma bodega/usuario/fecha/ítems/pesos) se intenta
# registrar de nuevo en un intervalo corto (reintento del webhook de Meta, doble
# llamada del bot o doble clic del usuario), NO se vuelve a insertar nada en
# movimientos_inventario: los métodos la detectan y devuelven {"duplicado": True}.
# Es la segunda capa; la primera es el dedupe de message_id en main.py.
_HUELLAS_RECIENTES: Dict[str, float] = {}
_HUELLA_TTL_SEGUNDOS = 30.0


def _verificar_duplicada(huella: str) -> bool:
    """True si una operación con esta huella YA se registró dentro de la
    ventana TTL (solo CONSULTA; NO graba nada — ver _registrar_huella).
    Purga antes las entradas expiradas."""
    ahora = time.time()
    for k in [k for k, ts in _HUELLAS_RECIENTES.items() if ahora - ts > _HUELLA_TTL_SEGUNDOS]:
        _HUELLAS_RECIENTES.pop(k, None)
    return huella in _HUELLAS_RECIENTES


def _registrar_huella(huella: str) -> None:
    """Graba la huella SOLO cuando el guardado en BD fue exitoso. Si una
    validación falla (stock insuficiente, conservación de masa…), la huella
    NO queda grabada y el reintento corregido del usuario se procesa normal;
    si el guardado falla, el usuario puede reintentar sin falso duplicado."""
    for k in [k for k, ts in _HUELLAS_RECIENTES.items() if time.time() - ts > _HUELLA_TTL_SEGUNDOS]:
        _HUELLAS_RECIENTES.pop(k, None)
    _HUELLAS_RECIENTES[huella] = time.time()


def _huella_operacion(*partes: Any) -> str:
    """Huella SHA-256 estable de una operación a partir de sus componentes
    (bodega, usuario, fecha, pares material/cantidad, merma, total). Las listas
    se ordenan antes de hashear para que un reintento con el mismo contenido —
    aunque llegue en otro orden— produzca la MISMA huella."""
    componentes: List[str] = []
    for p in partes:
        if isinstance(p, (list, tuple)):
            componentes.extend(sorted(str(i) for i in p))
        else:
            componentes.append(str(p))
    return hashlib.sha256("|".join(componentes).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MaterialDTO:
    id: int
    nombre: str
    tipo_material: str
    es_comercializable: bool


@dataclass(frozen=True)
class FuenteDTO:
    id: int
    nombre: str
    tipo_fuente: str


class InventarioServiceConValidacion:
    """Reglas de negocio e inserciones de inventario.

    Las operaciones agrupadas se guardan mediante la RPC
    ``registrar_lote_inventario`` definida en ``supabase/migracion_inventario.sql``.
    """

    def __init__(self, supabase_client: Client):
        self.supabase = supabase_client
        self.catalogo_materiales: Dict[str, MaterialDTO] = {}
        self.catalogo_fuentes: Dict[str, FuenteDTO] = {}
        self.recargar_catalogos()

    def recargar_catalogos(self) -> None:
        materiales = self.supabase.table("materiales").select(
            "id,nombre,tipo_material,es_comercializable"
        ).execute().data or []
        fuentes = self.supabase.table("fuentes_origen").select("id,nombre,tipo_fuente").execute().data or []

        self.catalogo_materiales = {}
        for fila in materiales:
            tipo = normalizar(str(fila.get("tipo_material") or "BRUTO")).upper()
            # Compatibilidad con el valor usado por la versión anterior.
            if tipo == "PROCESABLE":
                tipo = "SEMILIMPIO"
            # El antiguo estado DESPERDICIO (Basura/Tierra) se normaliza a MERMA.
            if tipo == "DESPERDICIO":
                tipo = "MERMA"
            self.catalogo_materiales[normalizar(fila["nombre"])] = MaterialDTO(
                id=fila["id"],
                nombre=fila["nombre"],
                tipo_material=tipo,
                es_comercializable=bool(fila.get("es_comercializable")),
            )

        self.catalogo_fuentes = {
            normalizar(fila["nombre"]): FuenteDTO(
                id=fila["id"], nombre=fila["nombre"], tipo_fuente=str(fila.get("tipo_fuente") or "EXTERNA_REVUELTO").upper()
            )
            for fila in fuentes
        }

    def _candidatos_aproximados(self, clave: str) -> List[str]:
        """Busca nombres del catálogo parecidos a `clave` (tolera errores de
        digitación). cutoff=0.75 evita falsos positivos entre materiales con
        nombres distintos; ajusta si hace falta más o menos estricto."""
        return difflib.get_close_matches(
            clave, self.catalogo_materiales.keys(), n=3, cutoff=0.75
        )

    def _clave_material(self, nombre: str) -> str:
        """Normaliza, unifica frases completas y aplica sinónimos de palabra,
        en ese orden, para obtener la clave de búsqueda en el catálogo."""
        return aplicar_sinonimos(aplicar_frases(normalizar(nombre)))

    @staticmethod
    def _frase_contenida(a: str, b: str) -> bool:
        """True si una frase completa contiene a la otra (sin recortar a
        palabras sueltas) y la parte corta es suficientemente larga (>=4
        caracteres) para no producir falsos positivos."""
        return (a in b or b in a) and min(len(a), len(b)) >= 4

    def obtener_material_por_nombre(self, nombre: str) -> Optional[MaterialDTO]:
        """Resuelve un material con prioridad EXACT-FIRST / LONGEST-MATCH sobre
        la FRASE COMPLETA recibida (jamás recortando a palabras individuales):

        1. Coincidencia EXACTA del nombre completo normalizado (sin sinónimos).
           'arreglo carter' → 'Arreglo Carter'; 'rechazo de aluminio' →
           'Rechazo de Aluminio'; 'carter' → 'Cárter'.
        2. Coincidencia exacta tras unificar frases y sinónimos de negocio
           ('arreglo grueso' → 'arreglo carter' SOLO si el paso 1 falló).
        3. Contención de la frase completa (longest match): el nombre recibido
           íntegro está contenido en un nombre del catálogo o viceversa; gana
           el nombre de catálogo MÁS LARGO y sin empates ('aluminio' →
           'Rechazo de Aluminio').
        4. Fuzzy (difflib) sobre el nombre completo; si hay varios candidatos
           igual de parecidos devuelve None (ambiguo) para no adivinar.
        """
        base = normalizar(nombre)
        if not base:
            return None

        # 1) Exacta sobre la cadena completa recibida.
        if base in self.catalogo_materiales:
            return self.catalogo_materiales[base]

        # 2) Exacta tras frases/sinónimos.
        clave = self._clave_material(nombre)
        if clave in self.catalogo_materiales:
            return self.catalogo_materiales[clave]

        # 3) Contención de la frase completa (longest match, sin empates).
        for candidato_txt in (base, clave):
            contenedores = []
            for k in self.catalogo_materiales:
                if not k or not self._frase_contenida(candidato_txt, k):
                    continue
                if candidato_txt in k:
                    # El usuario escribió una abreviatura de un nombre más
                    # largo del catálogo (ej. 'aluminio' ⊂ 'Rechazo de
                    # Aluminio'): siempre válido.
                    contenedores.append(k)
                else:
                    # k ⊂ candidato: el nombre del catálogo es MÁS CORTO que
                    # la frase del usuario (ej. 'bronce' ⊂ 'Rechazo de cobre
                    # y bronce'). Solo se acepta si longitudes comparables;
                    # si la frase es mucho más larga es una frase compuesta
                    # distinta y NO debe colapsar al material corto.
                    if len(k) >= 0.5 * len(candidato_txt):
                        contenedores.append(k)
            if contenedores:
                mejor = max(contenedores, key=len)
                if sum(1 for k in contenedores if len(k) == len(mejor)) == 1:
                    return self.catalogo_materiales[mejor]

        # 4) Fuzzy sobre el nombre completo (con detección de ambigüedad).
        for candidato_txt in (clave, base):
            candidatos = self._candidatos_aproximados(candidato_txt)
            if len(candidatos) == 1:
                return self.catalogo_materiales[candidatos[0]]
        return None

    def resolver_lista_materiales(self, texto: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Convierte el texto del usuario (lista con viñetas o líneas
        'Material Cantidad') en items del catálogo garantizando:

        - UNICIDAD 1:1: cada LÍNEA del texto se consume UNA sola vez y se
          asigna a UN SOLO material del catálogo. Es imposible que un mismo
          peso (ej. 501 kg) se replique en múltiples candidatos: no hay
          expansión de coincidencias múltiples.
        - CONSERVACIÓN DE FRASES COMPUESTAS: la resolución usa el nombre
          completo de la línea (exact-first/longest-match, ver
          obtener_material_por_nombre), sin recortar a 'Aluminio' cuando el
          usuario escribió 'Rechazo de Aluminio'.
        - Sin duplicados: líneas que resuelven al MISMO material se SUMAN en
          un único item (misma convención que fusionar_borrador).

        Devuelve (items, no_encontrados, merma_kg):
        - items = [{'material_nombre', 'cantidad_kg', 'precio_unitario'}]
          SOLO materiales comercializables (excluye tipo MERMA).
        - no_encontrados = nombres de línea que no se pudieron resolver
          (para feedback al usuario; NUNCA se omiten en silencio).
        - merma_kg = suma de líneas cuyo material es de tipo MERMA
          (ej. 'Basura'): van al descuento de merma, no a los ítems.
        """
        lineas = [l.strip().lstrip("*-•").strip()
                  for l in re.split(r"[\n,;]+", texto or "")]
        items: List[Dict[str, Any]] = []
        acumulados: Dict[str, float] = {}
        no_encontrados: List[str] = []
        merma_kg = 0.0
        for linea in lineas:
            if not linea:
                continue
            m = _LINEA_MATERIAL_CANTIDAD.match(linea)
            if not m:
                continue
            nombre, cantidad = m.group(1).strip(), float(m.group(2).replace(",", "."))
            # Unicidad: UNA resolución por línea; se consume y se avanza.
            mat = self.obtener_material_por_nombre(nombre)
            if mat is None:
                # Se reporta nombre + cantidad para que el mensaje de alerta
                # muestre la línea completa (ej. 'Rechazo de cobre y bronce 69').
                no_encontrados.append(f"{nombre} {cantidad:g}")
                continue
            # Clasificación de merma: los materiales de tipo MERMA (ej.
            # 'Basura') se acumulan en merma_kg, jamás como ítem vendible.
            if (mat.tipo_material or "").upper() == "MERMA":
                merma_kg += cantidad
                continue
            clave = normalizar(mat.nombre)
            if clave in acumulados:
                acumulados[clave] += cantidad
                for it in items:
                    if normalizar(it["material_nombre"]) == clave:
                        it["cantidad_kg"] = acumulados[clave]
                        break
            else:
                acumulados[clave] = cantidad
                items.append({
                    "material_nombre": mat.nombre,
                    "cantidad_kg": cantidad,
                    "precio_unitario": 0.0,
                })
        return items, no_encontrados, merma_kg

    def _material_obligatorio(self, nombre: str) -> MaterialDTO:
        material = self.obtener_material_por_nombre(nombre)
        if material:
            return material
        clave = self._clave_material(nombre)
        candidatos = self._candidatos_aproximados(clave)
        if len(candidatos) > 1:
            opciones = ", ".join(self.catalogo_materiales[c].nombre for c in candidatos)
            raise ValueError(
                f"'{nombre}' es ambiguo, se parece a varios materiales del catálogo: "
                f"{opciones}. Corrige el nombre para que coincida claramente con uno solo."
            )
        raise ValueError(
            f"El material '{nombre}' no existe en el catálogo y no encontré uno "
            f"suficientemente parecido. Verifica el nombre y vuelve a enviarlo."
        )

    def obtener_fuente_por_nombre(self, nombre: str) -> Optional[FuenteDTO]:
        return self.catalogo_fuentes.get(normalizar(nombre))

    def _fuente_obligatoria(self, nombre: str) -> FuenteDTO:
        fuente = self.obtener_fuente_por_nombre(nombre)
        if not fuente:
            raise ValueError(f"La fuente '{nombre}' no existe en fuentes_origen.")
        return fuente

    def _fuente_por_tipo(self, tipo_fuente: str) -> FuenteDTO:
        fuente = next((x for x in self.catalogo_fuentes.values() if x.tipo_fuente == tipo_fuente), None)
        if not fuente:
            raise ValueError(f"No existe una fuente configurada de tipo {tipo_fuente}.")
        return fuente

    @staticmethod
    def validar_fecha(fecha_str: str) -> str:
        try:
            fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ValueError("La fecha debe tener formato YYYY-MM-DD.")
        if fecha > date.today():
            raise ValueError("La fecha de operación no puede estar en el futuro.")
        return fecha.isoformat()

    @staticmethod
    def _cantidad(valor: Any, campo: str = "cantidad_kg") -> float:
        try:
            cantidad = float(valor)
        except (TypeError, ValueError):
            raise ValueError(f"{campo} debe ser numérico.")
        if cantidad <= 0:
            raise ValueError(f"{campo} debe ser mayor que cero.")
        return cantidad

    def obtener_saldo(self, bodega_id: int, material_id: int) -> float:
        filas = self.supabase.table("movimientos_inventario").select("cantidad_kg").eq(
            "bodega_id", bodega_id
        ).eq("material_id", material_id).execute().data or []
        return sum(float(fila["cantidad_kg"]) for fila in filas)

    def obtener_saldos_bodega(self, bodega_id: int) -> List[Dict[str, Any]]:
        filas = self.supabase.table("movimientos_inventario").select(
            "material_id,cantidad_kg,materiales(nombre)"
        ).eq("bodega_id", bodega_id).execute().data or []
        totales: Dict[str, float] = {}
        for fila in filas:
            nombre = fila.get("materiales", {}).get("nombre", "Desconocido")
            totales[nombre] = totales.get(nombre, 0.0) + float(fila["cantidad_kg"])
        saldos = [{"material": nombre, "saldo_kg": round(saldo, 2)}
                  for nombre, saldo in totales.items() if saldo]
        # Requisito: el informe de inventario incluye la lista COMPLETA de
        # materiales con stock ordenada ALFABÉTICAMENTE.
        saldos.sort(key=lambda x: normalizar(x["material"]))
        return saldos

    def obtener_movimientos_material(self, *, bodega_id: int, material_nombre: str,
                                     fecha_desde: Optional[str] = None,
                                     fecha_hasta: Optional[str] = None) -> Dict[str, Any]:
        """Historial de movimientos de un material, con saldo acumulado.
        Si se filtra por fecha_desde, el saldo_inicial refleja el stock
        acumulado ANTES de esa fecha, para que el saldo mostrado siga siendo real."""
        material = self._material_obligatorio(material_nombre)
        saldo_inicial = 0.0
        if fecha_desde:
            previos = self.supabase.table("movimientos_inventario").select("cantidad_kg").eq(
                "bodega_id", bodega_id).eq("material_id", material.id).lt("fecha_operacion", fecha_desde).execute().data or []
            saldo_inicial = sum(float(f["cantidad_kg"]) for f in previos)

        query = self.supabase.table("movimientos_inventario").select(
            "fecha_operacion,tipo_movimiento,cantidad_kg,observaciones,fuentes_origen(nombre)"
        ).eq("bodega_id", bodega_id).eq("material_id", material.id).order("fecha_operacion").order("fecha")
        if fecha_desde:
            query = query.gte("fecha_operacion", fecha_desde)
        if fecha_hasta:
            query = query.lte("fecha_operacion", fecha_hasta)
        filas = query.execute().data or []

        saldo = saldo_inicial
        movimientos = []
        for fila in filas:
            saldo += float(fila["cantidad_kg"])
            movimientos.append({
                "fecha": fila["fecha_operacion"],
                "tipo": fila["tipo_movimiento"],
                "cantidad_kg": float(fila["cantidad_kg"]),
                "fuente": (fila.get("fuentes_origen") or {}).get("nombre"),
                "saldo_acumulado": round(saldo, 2),
            })
        return {"material": material.nombre, "saldo_inicial": round(saldo_inicial, 2), "movimientos": movimientos}

    def obtener_reporte_diario_texto(self, bodega_id: int, fecha_operacion: str) -> str:
        """Resumen legible para WhatsApp de todos los movimientos de una bodega en un día."""
        fecha = self.validar_fecha(fecha_operacion)
        filas = self.supabase.table("movimientos_inventario").select(
            "tipo_movimiento,cantidad_kg,materiales(nombre),fuentes_origen(nombre)"
        ).eq("bodega_id", bodega_id).eq("fecha_operacion", fecha).execute().data or []
        mermas = self.supabase.table("mermas_proceso").select("cantidad_kg,tipo_merma").eq(
            "bodega_id", bodega_id
        ).eq("fecha_operacion", fecha).execute().data or []
        if not filas and not mermas:
            return f"No hay movimientos registrados en la Bodega #{bodega_id} para {fecha}."

        entradas, entradas_directas, compras, seleccionados, ventas = [], [], [], [], []
        total_entradas_revuelto = 0.0
        total_entradas_directas = 0.0
        total_resultados = 0.0
        revuelto_procesado = 0.0
        for fila in filas:
            material = (fila.get("materiales") or {}).get("nombre", "Material desconocido")
            fuente = (fila.get("fuentes_origen") or {}).get("nombre")
            cantidad = float(fila["cantidad_kg"])
            tipo = fila["tipo_movimiento"]
            if tipo == TipoTransaccion.ENTRADA_BRUTA.value:
                if normalizar(material) == "revuelto":
                    total_entradas_revuelto += cantidad
                    entradas.append(f"• {fuente or 'Sin fuente'}: +{cantidad:,.2f} kg")
                else:
                    total_entradas_directas += cantidad
                    entradas_directas.append(f"• +{cantidad:,.2f} kg de {material}{f' ({fuente})' if fuente else ''}")
            elif tipo in (TipoTransaccion.COMPRA.value, "COMPRA_DIRECTA"):
                compras.append(f"• +{cantidad:,.2f} kg de {material}{f' ({fuente})' if fuente else ''}")
            elif tipo == TipoTransaccion.TRANSFORMACION.value and cantidad < 0:
                revuelto_procesado += abs(cantidad)
            elif tipo == TipoTransaccion.TRANSFORMACION.value:
                total_resultados += cantidad
                seleccionados.append(f"• +{cantidad:,.2f} kg de {material}")
            elif tipo in (TipoTransaccion.VENTA.value, TipoTransaccion.DESPACHO.value):
                ventas.append(f"• {cantidad:,.2f} kg de {material}")

        lineas = [f"📋 Reporte diario — Bodega #{bodega_id}", f"Fecha: {fecha}"]
        if entradas:
            lineas += ["", f"*Entradas de Revuelto: {total_entradas_revuelto:,.2f} kg*"] + entradas
        if entradas_directas:
            lineas += ["", f"*Entradas directas: {total_entradas_directas:,.2f} kg*"] + entradas_directas
        if compras:
            lineas += ["", "*Compras directas*"] + compras
        if revuelto_procesado:
            lineas += ["", f"*Revuelto procesado:* {revuelto_procesado:,.2f} kg"]
        if seleccionados:
            lineas += [f"*Resultados de selección: {total_resultados:,.2f} kg*"] + seleccionados
        if ventas:
            lineas += ["", "*Ventas y despachos*"] + ventas
        total_merma = sum(float(x["cantidad_kg"]) for x in mermas)
        if total_merma:
            lineas += ["", f"*Merma:* {total_merma:,.2f} kg"]
        return "\n".join(lineas)

    def obtener_o_crear_cliente(self, *, nombre: str, documento: Optional[str] = None,
                                telefono: Optional[str] = None, direccion: Optional[str] = None) -> Dict[str, Any]:
        """Busca un cliente por documento (si se dio) o por nombre; si no existe, lo crea."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("Toda venta debe indicar el nombre del cliente.")
        if documento:
            existente = self.supabase.table("clientes").select("*").eq("identificacion", documento).execute().data
            if existente:
                return existente[0]
        else:
            existente = self.supabase.table("clientes").select("*").ilike("nombre", nombre).execute().data
            if existente:
                return existente[0]
        nuevo = self.supabase.table("clientes").insert({
            "nombre": nombre, "identificacion": documento, "telefono": telefono, "direccion": direccion,
        }).execute().data
        if not nuevo:
            raise ValueError("No se pudo registrar el cliente.")
        return nuevo[0]

    def obtener_conductor_por_nombre(self, nombre: str) -> Optional[Dict[str, Any]]:
        """Busca un conductor por nombre en la tabla 'conductores' (tabla espejo de clientes)."""
        filas = self.supabase.table("conductores").select("*").ilike("nombre", (nombre or "").strip()).execute().data
        return filas[0] if filas else None

    def obtener_o_crear_conductor(self, *, nombre: str, identificacion: Optional[str] = None,
                                  placa: Optional[str] = None, telefono: Optional[str] = None) -> Dict[str, Any]:
        """Módulo espejo de 'obtener_o_crear_cliente' pero para la tabla CONDUCTORES.

        Busca por identificación (si se dio) o por nombre; si el conductor ya existe,
        completa en su registro los campos que le falten; si no existe, lo crea.
        """
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("Toda venta debe indicar el nombre del conductor.")
        existente = None
        if identificacion:
            filas = self.supabase.table("conductores").select("*").eq("identificacion", identificacion).execute().data
            if filas:
                existente = filas[0]
        if not existente:
            filas = self.supabase.table("conductores").select("*").ilike("nombre", nombre).execute().data
            if filas:
                existente = filas[0]
        if existente:
            # Si el conductor ya existe, se completan SOLO los campos que le falten.
            cambios = {}
            if not existente.get("identificacion") and identificacion:
                cambios["identificacion"] = identificacion
            if not existente.get("placa") and placa:
                cambios["placa"] = placa
            if not existente.get("telefono") and telefono:
                cambios["telefono"] = telefono
            if cambios:
                actualizado = self.supabase.table("conductores").update(cambios).eq("id", existente["id"]).execute().data
                existente = actualizado[0] if actualizado else existente
            return existente
        nuevo = self.supabase.table("conductores").insert({
            "nombre": nombre, "identificacion": identificacion, "placa": placa, "telefono": telefono,
        }).execute().data
        if not nuevo:
            raise ValueError("No se pudo registrar el conductor.")
        return nuevo[0]

    def buscar_cliente_existente(self, *, identificacion: Optional[str] = None,
                                 telefono: Optional[str] = None,
                                 nombre: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Solo búsqueda (sin crear): devuelve el cliente que coincida por
        identificacion, luego telefono y luego nombre (en ese orden)."""
        if identificacion:
            filas = self.supabase.table("clientes").select("*").eq("identificacion", identificacion).execute().data
            if filas:
                return filas[0]
        if telefono:
            filas = self.supabase.table("clientes").select("*").eq("telefono", telefono).execute().data
            if filas:
                return filas[0]
        if nombre:
            filas = self.supabase.table("clientes").select("*").ilike("nombre", (nombre or "").strip()).execute().data
            if filas:
                return filas[0]
        return None

    def buscar_conductor_existente(self, *, identificacion: Optional[str] = None,
                                    placa: Optional[str] = None,
                                    nombre: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Solo búsqueda (sin crear): devuelve el conductor que coincida por
        identificacion, placa o nombre (en ese orden)."""
        if identificacion:
            filas = self.supabase.table("conductores").select("*").eq("identificacion", identificacion).execute().data
            if filas:
                return filas[0]
        if placa:
            filas = self.supabase.table("conductores").select("*").ilike("placa", f"%{placa}%").execute().data
            if filas:
                return filas[0]
        if nombre:
            filas = self.supabase.table("conductores").select("*").ilike("nombre", (nombre or "").strip()).execute().data
            if filas:
                return filas[0]
        return None

    def registrar_cliente(self, *, nombre: str, identificacion: Optional[str] = None,
                          telefono: Optional[str] = None,
                          direccion: Optional[str] = None) -> Dict[str, Any]:
        """Crea un cliente (tabla 'clientes'). Lanza ValueError si ya existe
        uno con esa identificación."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("El cliente debe tener un nombre.")
        existente = self.supabase.table("clientes").select("*").eq("identificacion", identificacion).execute().data
        if existente:
            raise ValueError(f"Ya existe un registro con la identificación {identificacion}.")
        nuevo = self.supabase.table("clientes").insert({
            "nombre": nombre, "identificacion": identificacion,
            "telefono": telefono, "direccion": direccion,
        }).execute().data
        if not nuevo:
            raise ValueError("No se pudo registrar el cliente.")
        return nuevo[0]

    def registrar_conductor(self, *, nombre: str, identificacion: Optional[str] = None,
                             placa: Optional[str] = None,
                             telefono: Optional[str] = None) -> Dict[str, Any]:
        """Crea un conductor (tabla 'conductores'). Lanza ValueError si ya existe
        uno con esa identificación."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("El conductor debe tener un nombre.")
        if identificacion:
            existente = self.supabase.table("conductores").select("*").eq("identificacion", identificacion).execute().data
            if existente:
                raise ValueError(f"Ya existe un registro con la identificación {identificacion}.")
        nuevo = self.supabase.table("conductores").insert({
            "nombre": nombre, "identificacion": identificacion,
            "placa": placa, "telefono": telefono,
        }).execute().data
        if not nuevo:
            raise ValueError("No se pudo registrar el conductor.")
        return nuevo[0]

    def registrar_material(self, *, nombre: str, tipo_material: str = "LIMPIO",
                          es_comercializable: bool = True) -> Dict[str, Any]:
        """Crea un material (tabla 'materiales'), recarga el catálogo en memoria
        (recargar_catalogos) para no reiniciar la app, y devuelve la fila.
        Lanza ValueError si ya existe un material con ese nombre."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("El material debe tener un nombre.")
        tipo = normalizar(tipo_material or "LIMPIO").upper()
        if tipo == "DESPERDICIO":
            tipo = "MERMA"
        existente = self.supabase.table("materiales").select("*").eq("nombre", nombre).execute().data
        if existente:
            raise ValueError(f"Ya existe un material con el nombre '{nombre}'.")
        nuevo = self.supabase.table("materiales").insert({
            "nombre": nombre, "tipo_material": tipo, "es_comercializable": bool(es_comercializable),
        }).execute().data
        if not nuevo:
            raise ValueError("No se pudo registrar el material.")
        # Actualizar el catálogo en memoria sin reiniciar la app.
        self.recargar_catalogos()
        return nuevo[0]

    def _movimiento(self, *, usuario_id: int, bodega_id: int, material_id: int,
                    tipo: TipoTransaccion, cantidad: float, fecha: str,
                    lote_id: str, fuente_id: Optional[int] = None,
                    observaciones: Optional[str] = None,
                    precio_unitario: float = 0.0) -> Dict[str, Any]:
        return {
            "usuario_id": usuario_id, "bodega_id": bodega_id, "material_id": material_id,
            "fuente_id": fuente_id, "tipo_movimiento": tipo.value, "cantidad_kg": cantidad,
            "precio_unitario": precio_unitario, "fecha_operacion": fecha,
            "lote_operacion_id": lote_id, "observaciones": observaciones,
        }

    def _guardar_lote(self, movimientos: List[Dict[str, Any]], mermas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Una RPC ejecuta ambas inserciones en una misma transacción PostgreSQL.
        respuesta = self.supabase.rpc("registrar_lote_inventario", {
            "p_movimientos": movimientos, "p_mermas": mermas
        }).execute()
        return respuesta.data or []

    def registrar_entrada_revuelto(self, *, bodega_id: int, usuario_id: int, fecha_operacion: str,
                                   entradas: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        fecha = self.validar_fecha(fecha_operacion)
        revuelto = self._material_obligatorio("Revuelto")
        if revuelto.tipo_material != TipoMaterial.BRUTO.value:
            raise ValueError("El material 'Revuelto' debe estar clasificado como BRUTO.")
        lote_id = str(uuid.uuid4())
        movimientos = []
        for entrada in entradas:
            fuente = self._fuente_obligatoria(entrada["fuente_nombre"])
            if fuente.tipo_fuente != "EXTERNA_REVUELTO":
                raise ValueError(f"La fuente '{fuente.nombre}' solo puede usarse para {fuente.tipo_fuente}, no para Revuelto.")
            cantidad = self._cantidad(entrada["cantidad_kg"])
            movimientos.append(self._movimiento(
                usuario_id=usuario_id, bodega_id=bodega_id, material_id=revuelto.id,
                fuente_id=fuente.id, tipo=TipoTransaccion.ENTRADA_BRUTA, cantidad=cantidad,
                fecha=fecha, lote_id=lote_id, observaciones=f"Ingreso de Revuelto desde {fuente.nombre}",
            ))
        registros = self._guardar_lote(movimientos, [])
        return {"lote_id": lote_id, "registros": registros}

    def registrar_compra_directa(self, *, bodega_id: int, usuario_id: int, fecha_operacion: str,
                                 fuente_nombre: str, items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        fecha, fuente, lote_id = self.validar_fecha(fecha_operacion), self._fuente_obligatoria(fuente_nombre), str(uuid.uuid4())
        if fuente.tipo_fuente != "COMPRA":
            raise ValueError(f"La fuente '{fuente.nombre}' no está configurada como fuente de compra.")
        movimientos = []
        for item in items:
            material = self._material_obligatorio(item["material_nombre"])
            if material.tipo_material == TipoMaterial.BRUTO.value:
                raise ValueError("Las compras directas deben indicar el material limpio o semilimpio, no Revuelto.")
            movimientos.append(self._movimiento(
                usuario_id=usuario_id, bodega_id=bodega_id, material_id=material.id, fuente_id=fuente.id,
                tipo=TipoTransaccion.COMPRA, cantidad=self._cantidad(item["cantidad_kg"]), fecha=fecha,
                lote_id=lote_id, precio_unitario=float(item.get("precio_unitario") or 0),
                observaciones="Compra directa",
            ))
        return {"lote_id": lote_id, "registros": self._guardar_lote(movimientos, [])}

    def registrar_ajuste_inventario(self, *, bodega_id: int, usuario_id: int, fecha_operacion: str,
                                    items: Iterable[Dict[str, Any]], motivo: Optional[str] = None) -> Dict[str, Any]:
        """Entrada directa de materiales que no proviene de compra ni de
        selección de Revuelto: carga de inventario inicial o corrección de stock."""
        fecha, lote_id = self.validar_fecha(fecha_operacion), str(uuid.uuid4())
        validados = [(self._material_obligatorio(item["material_nombre"]), self._cantidad(item["cantidad_kg"])) for item in items]
        movimientos = [self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=material.id,
            tipo=TipoTransaccion.AJUSTE_INVENTARIO, cantidad=cantidad, fecha=fecha, lote_id=lote_id,
            observaciones=motivo or "Ajuste/ingreso directo de inventario",
        ) for material, cantidad in validados]
        return {"lote_id": lote_id, "registros": self._guardar_lote(movimientos, [])}
    
    def registrar_seleccion_revuelto(self, *, bodega_id: int, usuario_id: int, fecha_operacion: str,
                                    resultados: Iterable[Dict[str, Any]], merma_kg: float,
                                    cantidad_revuelto_procesada: Optional[float] = None) -> Dict[str, Any]:
        fecha, revuelto, lote_id = self.validar_fecha(fecha_operacion), self._material_obligatorio("Revuelto"), str(uuid.uuid4())
        fuente_seleccion = self._fuente_por_tipo("PROCESO_SELECCION")
        resultados_validados = []
        for item in resultados:
            material, cantidad = self._material_obligatorio(item["material_nombre"]), self._cantidad(item["cantidad_kg"])
            if material.tipo_material == TipoMaterial.BRUTO.value:
                raise ValueError("Un resultado de selección no puede ser material BRUTO.")
            resultados_validados.append((material, cantidad))
        merma = float(merma_kg or 0)
        if merma < 0:
            raise ValueError("La merma no puede ser negativa.")
        total_resultados = sum(cantidad for _, cantidad in resultados_validados)
        total_procesado = self._cantidad(cantidad_revuelto_procesada) if cantidad_revuelto_procesada else total_resultados + merma
        if abs(total_procesado - total_resultados - merma) > 0.01:
            raise ValueError("El Revuelto procesado debe ser igual a resultados aprovechables + merma.")
        # Protección 2 (idempotencia): la huella se VERIFICA antes de validar
        # stock (un duplicado real ya consumió el stock y fallaría aquí con un
        # error confuso) pero se GRABA solo tras el guardado exitoso.
        huella = _huella_operacion(
            "SELECCION", bodega_id, usuario_id, fecha,
            [(m.id, round(c, 2)) for m, c in resultados_validados],
            round(merma, 2), round(total_procesado, 2),
        )
        if _verificar_duplicada(huella):
            return {"duplicado": True, "lote_id": None, "registros": [],
                    "merma_kg": merma, "revuelto_descontado": total_procesado}
        disponible = self.obtener_saldo(bodega_id, revuelto.id)
        if disponible + 0.01 < total_procesado:
            raise ValueError(f"Stock insuficiente de Revuelto. Disponible: {disponible:.2f} kg; requerido: {total_procesado:.2f} kg.")
        movimientos = [self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=revuelto.id,
            tipo=TipoTransaccion.TRANSFORMACION, cantidad=-total_procesado, fecha=fecha, lote_id=lote_id,
            observaciones="Salida de Revuelto por selección",
        )]
        movimientos.extend(self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=material.id,
            fuente_id=fuente_seleccion.id, tipo=TipoTransaccion.TRANSFORMACION, cantidad=cantidad, fecha=fecha, lote_id=lote_id,
            observaciones="Resultado de selección de Revuelto",
        ) for material, cantidad in resultados_validados)
        mermas = [] if not merma else [{
            "lote_operacion_id": lote_id, "bodega_id": bodega_id, "usuario_id": usuario_id,
            "material_origen_id": revuelto.id, "cantidad_kg": merma, "tipo_merma": "BASURA_TIERRA",
            "fecha_operacion": fecha, "observaciones": "Merma de selección; no genera stock vendible",
        }]
        registros = self._guardar_lote(movimientos, mermas)
        _registrar_huella(huella)
        return {"lote_id": lote_id, "registros": registros, "merma_kg": merma,
                "revuelto_descontado": total_procesado}

    def registrar_registro_diario(self, *, bodega_id: int, usuario_id: int, fecha_operacion: str,
                                  entradas: Iterable[Dict[str, Any]], resultados: Iterable[Dict[str, Any]],
                                  merma_kg: float, cantidad_revuelto_procesada: Optional[float] = None) -> Dict[str, Any]:
        """Registra entradas de Revuelto y su selección en un único lote atómico."""
        fecha, revuelto, lote_id = self.validar_fecha(fecha_operacion), self._material_obligatorio("Revuelto"), str(uuid.uuid4())
        fuente_seleccion = self._fuente_por_tipo("PROCESO_SELECCION")
        entradas_validadas = [(self._fuente_obligatoria(x["fuente_nombre"]), self._cantidad(x["cantidad_kg"])) for x in entradas]
        for fuente, _ in entradas_validadas:
            if fuente.tipo_fuente != "EXTERNA_REVUELTO":
                raise ValueError(f"La fuente '{fuente.nombre}' no es una fuente externa de Revuelto.")
        resultados_validados = []
        for item in resultados:
            material, cantidad = self._material_obligatorio(item["material_nombre"]), self._cantidad(item["cantidad_kg"])
            if material.tipo_material == TipoMaterial.BRUTO.value:
                raise ValueError("Un resultado de selección no puede ser material BRUTO.")
            resultados_validados.append((material, cantidad))
        merma = float(merma_kg or 0)
        if merma < 0:
            raise ValueError("La merma no puede ser negativa.")
        total_resultados = sum(x[1] for x in resultados_validados)
        total_procesado = self._cantidad(cantidad_revuelto_procesada) if cantidad_revuelto_procesada else total_resultados + merma
        if abs(total_procesado - total_resultados - merma) > 0.01:
            raise ValueError("El Revuelto procesado debe ser igual a resultados aprovechables + merma.")
        entrada_total = sum(x[1] for x in entradas_validadas)
        disponible = self.obtener_saldo(bodega_id, revuelto.id)
        if disponible + entrada_total + 0.01 < total_procesado:
            raise ValueError(f"Stock insuficiente de Revuelto. Existente: {disponible:.2f} kg; entradas: {entrada_total:.2f} kg; requerido: {total_procesado:.2f} kg.")
        # Protección 2 (idempotencia): verificación temprana; la huella se
        # graba tras el guardado exitoso (ver _registrar_huella).
        huella = _huella_operacion(
            "REGISTRO_DIARIO", bodega_id, usuario_id, fecha,
            [(f.id, round(c, 2)) for f, c in entradas_validadas],
            [(m.id, round(c, 2)) for m, c in resultados_validados],
            round(merma, 2), round(total_procesado, 2),
        )
        if _verificar_duplicada(huella):
            return {"duplicado": True, "lote_id": None, "registros": [],
                    "merma_kg": merma, "revuelto_descontado": total_procesado}
        movimientos = [self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=revuelto.id, fuente_id=fuente.id,
            tipo=TipoTransaccion.ENTRADA_BRUTA, cantidad=cantidad, fecha=fecha, lote_id=lote_id,
            observaciones=f"Ingreso de Revuelto desde {fuente.nombre}",
        ) for fuente, cantidad in entradas_validadas]
        movimientos.append(self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=revuelto.id,
            tipo=TipoTransaccion.TRANSFORMACION, cantidad=-total_procesado, fecha=fecha, lote_id=lote_id,
            observaciones="Salida de Revuelto por selección",
        ))
        movimientos.extend(self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=material.id,
            fuente_id=fuente_seleccion.id, tipo=TipoTransaccion.TRANSFORMACION, cantidad=cantidad, fecha=fecha, lote_id=lote_id,
            observaciones="Resultado de selección de Revuelto",
        ) for material, cantidad in resultados_validados)
        mermas = [] if not merma else [{
            "lote_operacion_id": lote_id, "bodega_id": bodega_id, "usuario_id": usuario_id,
            "material_origen_id": revuelto.id, "cantidad_kg": merma, "tipo_merma": "BASURA_TIERRA",
            "fecha_operacion": fecha, "observaciones": "Merma de selección; no genera stock vendible",
        }]
        registros = self._guardar_lote(movimientos, mermas)
        _registrar_huella(huella)
        return {"lote_id": lote_id, "registros": registros, "merma_kg": merma,
                "revuelto_descontado": total_procesado}

    def registrar_transformacion_material(
            self, *, bodega_id: int, usuario_id: int, fecha_operacion: str,
            material_origen_nombre: str, resultados: Iterable[Dict[str, Any]],
            merma_kg: float = 0.0, cantidad_procesada: Optional[float] = None,
            material_merma_nombre: Optional[str] = None,
            nombre_proceso: str = "Transformación de material",
    ) -> Dict[str, Any]:
        """Transforma un material de origen en varios productos (limpios,
        semilimpios y merma) descontando el 100% de lo ingresado, garantizando
        la conservación de masa:  cantidad_procesada == Σ salidas + merma.

        Reglas por estado del origen:
        - Regla 1 (primaria/selección): origen BRUTO (sólo 'Revuelto') →
          limpios/semilimpios/merma, todo descontado del Revuelto.
        - Regla 2 (re-transformación, ej. quema de Cable): origen SEMILIMPIO →
          semilimpios + merma. NO afecta al Revuelto/Bruto.
        - Regla 3 (selección técnica/desmonte, ej. Arreglo Carter): origen
          SEMILIMPIO → limpios + semilimpios + merma, descontando 100% origen.

        La merma se trata como material MERMA del catálogo (p. ej. 'Basura' /
        'Tierra') que acumula stock vendible; si no existe se guarda en
        ``mermas_proceso`` (heredado) sin generar stock."""

        fecha = self.validar_fecha(fecha_operacion)
        origen = self._material_obligatorio(material_origen_nombre)
        lote_id = str(uuid.uuid4())

        if origen.tipo_material not in (TipoMaterial.BRUTO.value, TipoMaterial.SEMILIMPIO.value):
            raise ValueError(
                "Solo pueden transformarse materiales BRUTO (Revuelto) o SEMILIMPIO. "
                f"'{origen.nombre}' está clasificado como {origen.tipo_material}."
            )
        if origen.tipo_material == TipoMaterial.BRUTO.value and normalizar(origen.nombre) != "revuelto":
            raise ValueError("La transformación primaria solo puede partir de 'Revuelto' (BRUTO).")

        merma = float(merma_kg or 0)
        if merma < 0:
            raise ValueError("La merma no puede ser negativa.")

        resultados_validados = []
        for item in resultados:
            material, cantidad = self._material_obligatorio(item["material_nombre"]), self._cantidad(item["cantidad_kg"])
            if material.id == origen.id:
                raise ValueError(f"'{origen.nombre}' no puede generarse a sí mismo.")
            if material.tipo_material == TipoMaterial.BRUTO.value:
                raise ValueError("Un producto de transformación no puede ser BRUTO (Revuelto).")
            resultados_validados.append((material, cantidad))

        total_resultados = sum(c for _, c in resultados_validados)

        # Conservación de masa
        total_procesado = self._cantidad(cantidad_procesada) if cantidad_procesada else total_resultados + merma
        if abs(total_procesado - total_resultados - merma) > 0.01:
            raise ValueError(
                f"Conservación de masa: procesado ({total_procesado:,.2f} kg) debe ser "
                f"igual a resultados ({total_resultados:,.2f} kg) + merma ({merma:,.2f} kg)."
            )

        # Stock suficiente del origen
        disponible = self.obtener_saldo(bodega_id, origen.id)
        if disponible + 0.01 < total_procesado:
            raise ValueError(
                f"Stock insuficiente de '{origen.nombre}'. Disponible: {disponible:,.2f} kg; "
                f"requerido: {total_procesado:,.2f} kg."
            )
        # Protección 2 (idempotencia): verificación temprana; la huella se
        # graba tras el guardado exitoso (ver _registrar_huella).
        huella = _huella_operacion(
            "TRANSFORMACION", bodega_id, usuario_id, fecha, origen.id,
            [(m.id, round(c, 2)) for m, c in resultados_validados],
            round(merma, 2), round(total_procesado, 2),
        )
        if _verificar_duplicada(huella):
            return {"duplicado": True, "lote_id": None, "registros": [], "merma_kg": merma,
                    "origen": origen.nombre, "tipo_origen": origen.tipo_material,
                    "materiales_salida": [mat.nombre for mat, _ in resultados_validados]}

        fuente_proceso = self._fuente_por_tipo("PROCESO_SELECCION")

        movimientos = [self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=origen.id,
            tipo=TipoTransaccion.TRANSFORMACION, cantidad=-total_procesado, fecha=fecha,
            lote_id=lote_id, observaciones=f"Salida de {origen.nombre} por {nombre_proceso}",
        )]
        movimientos.extend(self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=material.id,
            fuente_id=fuente_proceso.id, tipo=TipoTransaccion.TRANSFORMACION, cantidad=cantidad, fecha=fecha,
            lote_id=lote_id, observaciones=f"Resultado de {nombre_proceso} de {origen.nombre}",
        ) for material, cantidad in resultados_validados)

        mermas: List[Dict[str, Any]] = []
        if merma:
            merma_mat = self.obtener_material_por_nombre(material_merma_nombre or "Basura")
            if merma_mat and merma_mat.tipo_material == TipoMaterial.MERMA.value:
                movimientos.append(self._movimiento(
                    usuario_id=usuario_id, bodega_id=bodega_id, material_id=merma_mat.id,
                    fuente_id=fuente_proceso.id, tipo=TipoTransaccion.TRANSFORMACION, cantidad=merma, fecha=fecha,
                    lote_id=lote_id, observaciones=f"Merma (vendible) de {nombre_proceso} de {origen.nombre}",
                ))
            else:
                mermas = [{
                    "lote_operacion_id": lote_id, "bodega_id": bodega_id, "usuario_id": usuario_id,
                    "material_origen_id": origen.id, "cantidad_kg": merma, "tipo_merma": "BASURA_TIERRA",
                    "fecha_operacion": fecha,
                    "observaciones": f"Merma de {nombre_proceso}; sin material MERMA de catálogo para stock",
                }]

        registros = self._guardar_lote(movimientos, mermas)
        _registrar_huella(huella)
        return {
            "lote_id": lote_id, "registros": registros, "merma_kg": merma,
            "origen": origen.nombre, "tipo_origen": origen.tipo_material,
            "materiales_salida": [mat.nombre for mat, _ in resultados_validados],
        }

    def registrar_venta_multiple(self, *, bodega_id: int, usuario_id: int, fecha_operacion: str,
                                 items: Iterable[Dict[str, Any]], cliente: Optional[str] = None,
                                 cliente_documento: Optional[str] = None, cliente_telefono: Optional[str] = None,
                                 cliente_direccion: Optional[str] = None,
                                 cliente_conductor: Optional[str] = None,
                                 cliente_conductor_id: Optional[str] = None,
                                 cliente_placa: Optional[str] = None,
                                 cliente_conductor_telefono: Optional[str] = None) -> Dict[str, Any]:
        fecha, lote_id = self.validar_fecha(fecha_operacion), str(uuid.uuid4())
        cliente_registro = self.obtener_o_crear_cliente(
            nombre=cliente, documento=cliente_documento, telefono=cliente_telefono, direccion=cliente_direccion,
        )
        conductor_registro = None
        if cliente_conductor:
            conductor_registro = self.obtener_o_crear_conductor(
                nombre=cliente_conductor, identificacion=cliente_conductor_id,
                placa=cliente_placa, telefono=cliente_conductor_telefono,
            )
        validados = []
        for item in items:
            material, cantidad = self._material_obligatorio(item["material_nombre"]), self._cantidad(item["cantidad_kg"])
            if not material.es_comercializable:
                raise ValueError(f"'{material.nombre}' está marcado como no comercializable.")
            disponible = self.obtener_saldo(bodega_id, material.id)
            if disponible + 0.01 < cantidad:
                raise ValueError(f"Stock insuficiente de {material.nombre}. Disponible: {disponible:.2f} kg; requerido: {cantidad:.2f} kg.")
            validados.append((material, cantidad))
        movimientos = [self._movimiento(
            usuario_id=usuario_id, bodega_id=bodega_id, material_id=material.id,
            tipo=TipoTransaccion.VENTA, cantidad=-cantidad, fecha=fecha, lote_id=lote_id,
            observaciones=f"Venta/despacho a {cliente_registro['nombre']}",
        ) for material, cantidad in validados]
        registros = self._guardar_lote(movimientos, [])
        self.supabase.table("movimientos_inventario").update(
            {"cliente_id": cliente_registro["id"]}
        ).eq("lote_operacion_id", lote_id).execute()
        numero_remision = self.generar_numero_remision()
        self.registrar_remision(
            numero=numero_remision, 
            lote_operacion_id=lote_id, 
            cliente_id=cliente_registro["id"],
            conductor_id=(conductor_registro or {}).get("id"),
            bodega_id=bodega_id, 
            fecha_operacion=fecha,
            estado="ORDEN_SALIDA",
        )
        return {"lote_id": lote_id, "registros": registros, "cliente": cliente_registro,
                "conductor": conductor_registro, "numero_remision": numero_remision,
                "estado": "ORDEN_SALIDA"}

        
    def generar_numero_remision(self) -> str:
        """Calcula el número de la siguiente remisión revisando el último número
        realmente creado en la tabla 'remisiones' y generando 'último + 1'.

        A diferencia del RPC (que deja huecos, p. ej. porque simplemente suma 1
        al total de filas o usa una secuencia), aquí se toma el último número
        real existente y se devuelve el consecutivo siguiente sin dejar vacíos.
        """
        filas = self.supabase.table("remisiones").select("numero").execute() or []
        datos = filas.data if hasattr(filas, "data") else filas
        maximo = 0
        for fila in (datos or []):
            numero_txt = (fila.get("numero") or "").strip()
            # Se extrae el sufijo numérico final (ej. de "REM_112" -> 112).
            match = re.search(r"(\d+)\s*$", numero_txt)
            if match:
                valor = int(match.group(1))
                if valor > maximo:
                    maximo = valor
        siguiente = maximo + 1
        # Se conserva el ancho actual (mínimo 3 dígitos), ampliándolo si hace falta.
        ancho = max(3, len(str(maximo)))
        return f"REM_{str(siguiente).zfill(ancho)}"

    def registrar_remision(self, *, numero: str, lote_operacion_id: str, cliente_id: int, 
                           bodega_id: int, fecha_operacion: str,
                           conductor_id: Optional[int] = None,
                           estado: str = "ORDEN_SALIDA"):
        
        # La remisión solo guarda referencias: cliente_id y conductor_id.
        # Los datos completos del cliente y del conductor viven en sus respectivas tablas.
        # Flujo "Orden de Salida -> Remisión Aprobada": toda venta/despacho nace como
        # 'ORDEN_SALIDA' (sin precios) y solo Contabilidad la pasa a 'APROBADA'
        # (vía RPC aprobar_remision_con_precios) o 'ANULADA'.
        data = {
            "numero": numero,
            "lote_operacion_id": lote_operacion_id,
            "cliente_id": cliente_id,
            "conductor_id": conductor_id,
            "bodega_id": bodega_id,
            "fecha_operacion": fecha_operacion,
            "estado": estado,
        }
        
        self.supabase.table("remisiones").insert(data).execute()

    def obtener_cliente_por_nombre(self, nombre: str) -> Optional[Dict[str, Any]]:
        filas = self.supabase.table("clientes").select("*").ilike("nombre", (nombre or "").strip()).execute().data
        return filas[0] if filas else None

    def actualizar_cliente(self, cliente_id: int, campos: Dict[str, Any]) -> Dict[str, Any]:
        permitidos = {"nombre", "identificacion", "telefono", "direccion"}
        datos = {k: v for k, v in campos.items() if k in permitidos and v}
        if not datos:
            raise ValueError("No se indicó ningún dato válido para corregir.")
        actualizado = self.supabase.table("clientes").update(datos).eq("id", cliente_id).execute().data
        if not actualizado:
            raise ValueError("No se pudo actualizar el cliente.")
        return actualizado[0]

    def obtener_remision(self, numero: str) -> Optional[Dict[str, Any]]:
        """Busca una remisión por número tolerando variantes de escritura:
        '101', 'REM_101', 'rem 101', 'OS-1001', 'os_1001' (se prueban los
        candidatos en orden hasta encontrar coincidencia en BD)."""
        limpio = (numero or "").strip().upper().replace(" ", "")
        digitos = re.sub(r"\D", "", limpio)
        if limpio.startswith("REM_"):
            candidatos = [limpio, f"REM_{digitos}"] if digitos else [limpio]
        elif limpio.startswith("OS"):
            candidatos = [limpio, f"OS-{digitos}", f"OS_{digitos}", f"REM_{digitos}"] if digitos else [limpio]
        else:
            candidatos = [limpio, f"REM_{limpio}", f"REM_{digitos}"] if digitos else [limpio]
        vistos: set = set()
        candidatos = [c for c in candidatos if c and not (c in vistos or vistos.add(c))]
        remision = None
        for cand in candidatos:
            filas = self.supabase.table("remisiones").select("*").eq("numero", cand).execute().data
            if filas:
                remision = filas[0]
                break
        if not remision:
            return None
        movimientos = self.supabase.table("movimientos_inventario").select(
            "id,material_id,cantidad_kg,anulado,precio_unitario,materiales(nombre)"
        ).eq("lote_operacion_id", remision["lote_operacion_id"]).eq("anulado", False).execute().data or []
        remision["movimientos"] = movimientos
        return remision

    def obtener_ordenes_salida(self, bodega_id: int, limite: int = 3) -> List[Dict[str, Any]]:
        """Últimas remisiones en estado 'ORDEN_SALIDA' de una bodega: las órdenes
        de despacho que quedaron sin valorizar y esperan aprobación de Contabilidad.

        Devuelve las más recientes primero (id descendente), limitadas a `limite`.
        """
        filas = (self.supabase.table("remisiones")
                 .select("id,numero,fecha_operacion,estado,bodega_id")
                 .eq("estado", "ORDEN_SALIDA")
                 .eq("bodega_id", bodega_id)
                 .order("id", desc=True)
                 .limit(limite)
                 .execute().data or [])
        return filas

    def aprobar_remision_con_precios(self, remision_id: Any, vr_dolar_dia: float,
                                     precios: Dict[Any, float]) -> Dict[str, Any]:
        """Ejecuta la RPC 'aprobar_remision_con_precios' (transacción atómica):
        fija remisiones.vr_dolar_dia, marca estado='APROBADA' y guarda el
        precio_unitario (por kilogramo) de cada movimiento del lote.

        ⚠️ Tipos UUID (PostgREST): las PKs son UUID en PostgreSQL y PostgREST
        solo acepta representación JSON. `p_remision_id` se envía SIEMPRE como
        string (la RPC lo castea a ::uuid) y las llaves de `p_precios_items`
        como strings del id del movimiento (la RPC las castea a ::uuid).
        Enviar enteros aquí provocaba 'operator does not exist: uuid = text'
        (42883).
        """
        precios_items = {str(mov_id): float(p) for mov_id, p in precios.items()}
        respuesta = self.supabase.rpc("aprobar_remision_con_precios", {
            "p_remision_id": str(remision_id),
            "p_vr_dolar_dia": float(vr_dolar_dia),
            "p_precios_items": precios_items,
        }).execute()
        datos = getattr(respuesta, "data", None)
        if isinstance(datos, list):
            datos = datos[0] if datos else {}
        return datos if isinstance(datos, dict) else {}

    def obtener_datos_pdf_remision(self, numero: str) -> Dict[str, Any]:
        """Reúne todos los datos necesarios para regenerar el PDF de una remisión
        EXISTENTE conservando exactamente el mismo número correlativo.

        Devuelve la remisión tal cual está en la base (con las correcciones ya
        aplicadas por anular_o_actualizar_linea / actualizar_cantidad_linea /
        actualizar_cliente). El número usado es el original: NO se genera uno nuevo.
        """
        remision = self.obtener_remision(numero)
        if not remision:
            raise ValueError(f"No existe la remisión '{numero}'.")

        cliente = {}
        if remision.get("cliente_id"):
            filas = self.supabase.table("clientes").select("*").eq("id", remision["cliente_id"]).execute().data
            if filas:
                cliente = filas[0]

        conductor = {}
        if remision.get("conductor_id"):
            filas = self.supabase.table("conductores").select("*").eq("id", remision["conductor_id"]).execute().data
            if filas:
                conductor = filas[0]

        # Los movimientos de una venta se guardan con cantidad_kg negativa; el
        # PDF muestra valores positivos, igual que en la venta original. El
        # precio_unitario (si existe) permite renderizar el PDF 'APROBADA'
        # con conversiones a dólar; en 'ORDEN_SALIDA' viene vacío y el PDF
        # omite las columnas financieras.
        items = []
        for m in remision.get("movimientos", []):
            nombre = (m.get("materiales") or {}).get("nombre", "Material")
            items.append({
                "material_nombre": nombre,
                "cantidad_kg": abs(float(m["cantidad_kg"])),
                "precio_unitario": m.get("precio_unitario"),
            })

        return {
            "numero_remision": remision["numero"],
            "fecha_operacion": remision.get("fecha_operacion"),
            "bodega_id": remision.get("bodega_id"),
            "estado": remision.get("estado"),
            "vr_dolar_dia": remision.get("vr_dolar_dia"),
            "cliente": cliente,
            "conductor": conductor,
            "items": items,
        }

    def anular_remision_completa(self, numero: str, usuario_id: int) -> Dict[str, Any]:
        remision = self.obtener_remision(numero)
        if not remision:
            raise ValueError(f"No existe la remisión '{numero}'.")
        lote_reversa = str(uuid.uuid4())
        reversas = []
        for mov in remision["movimientos"]:
            reversas.append(self._movimiento(
                usuario_id=usuario_id, bodega_id=remision["bodega_id"], material_id=mov["material_id"],
                tipo=TipoTransaccion.ANULACION, cantidad=-float(mov["cantidad_kg"]), fecha=date.today().isoformat(),
                lote_id=lote_reversa, observaciones=f"Anulación total de {remision['numero']}",
            ))
            self.supabase.table("movimientos_inventario").update({"anulado": True}).eq("id", mov["id"]).execute()
        if reversas:
            self._guardar_lote(reversas, [])
        self.supabase.table("remisiones").update({"estado": "ANULADA"}).eq("id", remision["id"]).execute()
        return {"numero": remision["numero"], "lineas_anuladas": len(reversas)}

    def anular_o_actualizar_linea(self, *, numero: str, material_nombre: str, cantidad_kg: float,
                                  usuario_id: int) -> Dict[str, Any]:
        remision = self.obtener_remision(numero)
        if not remision:
            raise ValueError(f"No existe la remisión '{numero}'.")
        material = self._material_obligatorio(material_nombre)
        linea = next((m for m in remision["movimientos"] if m["material_id"] == material.id), None)
        if not linea:
            raise ValueError(f"'{material.nombre}' no está en la remisión {remision['numero']}.")
        cantidad_actual = abs(float(linea["cantidad_kg"]))
        if abs(cantidad_actual - cantidad_kg) <= 0.01:
            lote_reversa = str(uuid.uuid4())
            self._guardar_lote([self._movimiento(
                usuario_id=usuario_id, bodega_id=remision["bodega_id"], material_id=material.id,
                tipo=TipoTransaccion.ANULACION, cantidad=-float(linea["cantidad_kg"]), fecha=date.today().isoformat(),
                lote_id=lote_reversa, observaciones=f"Anulación de línea en {remision['numero']}",
            )], [])
            self.supabase.table("movimientos_inventario").update({"anulado": True}).eq("id", linea["id"]).execute()
            self.supabase.table("remisiones").update({"estado": "MODIFICADA"}).eq("id", remision["id"]).execute()
            return {"accion": "anulada", "material": material.nombre, "cantidad": cantidad_actual}
        return {"accion": "requiere_confirmacion", "material": material.nombre,
                "cantidad_actual": cantidad_actual, "cantidad_nueva": cantidad_kg,
                "movimiento_id": linea["id"]}

    def actualizar_cantidad_linea(self, *, movimiento_id: int, nueva_cantidad_kg: float) -> None:
        fila = self.supabase.table("movimientos_inventario").select("cantidad_kg,observaciones").eq("id", movimiento_id).execute().data
        if not fila:
            raise ValueError("No se encontró el movimiento a actualizar.")
        anterior = float(fila[0]["cantidad_kg"])
        signo = -1 if anterior < 0 else 1
        nota = f"{fila[0].get('observaciones') or ''} | Corregido de {abs(anterior):,.2f} a {nueva_cantidad_kg:,.2f} kg el {date.today().isoformat()}"
        self.supabase.table("movimientos_inventario").update({
            "cantidad_kg": signo * nueva_cantidad_kg, "observaciones": nota,
        }).eq("id", movimiento_id).execute()