"""Modelos Pydantic del agente (DeepSeek), prompt, fusion de borrador y validaciones."""
import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from pydantic import BaseModel, Field, field_validator

from core import config as _config
from core.config import BOGOTA, DEEPSEEK_API_KEY, inventario
from services.inventario_service import (
    TipoMaterial, borrador_para_nueva_lista, normalizar, normalizar_digitos,
)
from utils.parsers import (
    _limpiar_nombre_para_busqueda, _PALABRAS_CLAVE_PROCESO, _parsear_numero,
    parsear_fecha_colombiana, parsear_material_cantidad,
)

logger = logging.getLogger(__name__)


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
    """Llama al LLM. Si USE_OLLAMA=1 (o la variable de entorno esta activa),
    usa Ollama local; en caso contrario, usa la API de DeepSeek.

    El timeout del cliente HTTP (config.http_client) se amplia automaticamente
    a 180s al usar Ollama porque los modelos locales son mas lentos."""
    from core.config import OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_API_KEY
    import os as _os

    use_ollama = _os.getenv("USE_OLLAMA", "0").strip() in ("1", "true", "TRUE", "yes", "YES")

    if use_ollama:
        logger.info(f"🦙 Usando Ollama local ({OLLAMA_MODEL}) en {OLLAMA_BASE_URL}")
        # Si el cliente global tiene un timeout corto, creamos uno local con timeout largo
        # (los modelos locales de 7B pueden tardar 60-120s en responder).
        client = _config.http_client
        try:
            timeout_actual = getattr(client.timeout, "connect", 30) if client else 30
        except Exception:
            timeout_actual = 30
        if timeout_actual < 120:
            client = httpx.AsyncClient(timeout=httpx.Timeout(180.0))
            cerrar_local = True
        else:
            cerrar_local = False

        try:
            url = f"{OLLAMA_BASE_URL.rstrip('/')}/v1/chat/completions"
            respuesta = await client.post(
                url,
                headers={"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": OLLAMA_MODEL,
                    "temperature": 0,
                    "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": mensaje}],
                },
            )
            respuesta.raise_for_status()
            contenido = respuesta.json()["choices"][0]["message"]["content"]
            contenido = re.sub(r"^```(?:json)?\s*|\s*```$", "", contenido.strip(), flags=re.I)
            return RespuestaAgente.model_validate_json(contenido)
        finally:
            if cerrar_local:
                await client.aclose()
    else:
        assert _config.http_client is not None
        respuesta = await _config.http_client.post(
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
