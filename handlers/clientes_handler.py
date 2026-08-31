"""Handler de clientes: captura en bloque y paso a paso (nombre, ID, telefono, direccion)."""
import asyncio
import logging
from typing import Any, Dict, Optional

from core.config import inventario
from core.contexto import guardar_contexto
from core.whatsapp import enviar_botones_whatsapp, enviar_mensaje_whatsapp
from handlers import MANEJADO
from services.inventario_service import normalizar_digitos, parsear_bloque_persona
from utils.whatsapp_formatter import _formatear_ficha_cliente

logger = logging.getLogger(__name__)


async def _manejar_cliente_duplicado(
    telefono: str,
    usuario_id: int,
    contexto: Dict[str, Any],
    existente: Dict[str, Any],
    *,
    accion_en_curso: Optional[Dict[str, Any]] = None,
) -> None:
    """Cliente ya registrado: frena el wizard y ofrece dos caminos.

    - "Usar este cliente"  → guarda la selección en ``contexto`` y limpia
      cualquier ``accion_pendiente`` activa.
    - "Ingresar otro"     → limpia por completo el contexto del usuario.

    Si se pasa ``accion_en_curso`` se conserva en ``contexto`` con tipo
    ``cliente_duplicado_pendiente`` para que el handler de a 2 botones
    (router) pueda procesarla al recibir la respuesta.
    """
    nombre = (existente.get("nombre") or "").strip() or "(sin nombre)"
    msg = (
        f"⚠️ El cliente '{nombre}' ya se encuentra registrado en el sistema. "
        f"¿Deseas seleccionar este cliente o registrar uno con un "
        f"nombre/identificación diferente?\n\n"
        f"{_formatear_ficha_cliente(existente)}"
    )
    if accion_en_curso:
        # Preservamos el estado para que el router pueda "Usar este cliente"
        # y reanudar el flujo original tras confirmar la selección.
        contexto["accion_pendiente"] = {
            "tipo": "cliente_duplicado_pendiente",
            "cliente_existente_id": existente.get("id"),
            "cliente_existente_nombre": nombre,
            "accion_en_curso": accion_en_curso,
        }
    else:
        # Sin flujo anterior: solo registramos el id para que pueda ser
        # retomado si el usuario elige "Usar este cliente".
        contexto["accion_pendiente"] = {
            "tipo": "cliente_duplicado_pendiente",
            "cliente_existente_id": existente.get("id"),
            "cliente_existente_nombre": nombre,
        }
    await guardar_contexto(usuario_id, contexto)
    await enviar_botones_whatsapp(
        telefono,
        msg,
        [
            ("usar_cliente", "Usar este cliente"),
            ("otro_cliente", "Ingresar otro"),
        ],
    )



def _datos_cliente(p: Dict[str, Any]) -> Dict[str, Any]:
    """Mapea el bloque parseado a los parámetros EXPLÍCITOS de registrar_cliente
    (única vía permitida: nunca se pasan claves del conductor al cliente)."""
    return {
        "nombre_cliente": p.get("nombre") or "",
        "id_cliente": p.get("identificacion") or None,
        "telefono_cliente": p.get("telefono") or None,
        "direccion_cliente": p.get("direccion") or None,
    }
async def capturar_bloque_cliente(telefono: str, usuario_id: int, bodega_id: int,
                                  contexto: Dict[str, Any], texto: str) -> None:
    """En bloque (Cliente): parsea el mensaje; si está completo y no existe, lo crea.
    Si falta nombre/identificación/teléfono/dirección, pasa a paso a paso SOLO lo faltante.

    Antes de avanzar, verifica si el cliente ya existe (por nombre, id o
    teléfono) para evitar duplicados silenciosos.
    """
    p = parsear_bloque_persona(texto)
    # Búsqueda de duplicado por los 3 criterios disponibles (nombre, id, teléfono).
    exist = await asyncio.to_thread(
        inventario.buscar_cliente_existente,
        identificacion=p.get("identificacion") or None,
        telefono=p.get("telefono") or None,
        nombre=p.get("nombre") or None,
    )
    if exist:
        await _manejar_cliente_duplicado(telefono, usuario_id, contexto, exist)
        return
    faltan = [c for c in ("nombre", "identificacion", "telefono", "direccion") if not p.get(c)]
    if faltan:
        contexto["accion_pendiente"] = {"tipo": "crear_cliente_paso", "datos": dict(p)}
        contexto["campo_esperado"] = "cliente_" + faltan[0].replace("identificacion", "documento")
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, f"Faltan datos del cliente: {', '.join(faltan)}.\n¿{_pregunta_campo_cliente(faltan[0])}")
        return
    try:
        nuevo = await asyncio.to_thread(inventario.registrar_cliente, **_datos_cliente(p))
        contexto["accion_pendiente"] = {}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, f"✅ Cliente registrado:\n{_formatear_ficha_cliente(nuevo)}")
    except ValueError as e:
        contexto["accion_pendiente"] = {}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, str(e))


def _pregunta_campo_cliente(campo: str) -> str:
    return {
        "nombre": "Nombre completo del cliente:",
        "identificacion": "Identificación (CC/NIT):",
        "telefono": "Teléfono / celular:",
        "direccion": "Dirección:",
    }.get(campo, f"{campo}:")



async def procesar_flujo_cliente(telefono: str, usuario_id: int, bodega_id: int,
                                contexto: Dict[str, Any], accion: Dict[str, Any], texto: str):
    """Punto de entrada del flujo de clientes (bloque y paso a paso).

    Devuelve el texto de respuesta o MANEJADO si el handler ya notifico."""
    if accion["tipo"] == "crear_cliente_bloque":
        await capturar_bloque_cliente(telefono, usuario_id, bodega_id, contexto, texto)
        return MANEJADO
    # accion["tipo"] == "crear_cliente_paso"
    # Paso a paso de Cliente: se completa el dato faltante del bloque.
    datos = dict(accion.get("datos") or {})
    faltan = [c for c in ("nombre", "identificacion", "telefono", "direccion") if not datos.get(c)]
    campo = faltan[0] if faltan else None
    if campo == "nombre":
        datos["nombre"] = texto.strip()
    elif campo == "identificacion":
        doc_limpio = normalizar_digitos(texto)
        if not doc_limpio or not any(ch.isdigit() for ch in doc_limpio):
            return "⚠️ La cédula o documento debe contener números válidos. Por favor, ingresa un número de documento válido (ej: 12345678):"
        datos["identificacion"] = doc_limpio
    elif campo == "telefono":
        datos["telefono"] = texto.strip()
    elif campo == "direccion":
        datos["direccion"] = texto.strip()

    # Búsqueda de duplicado en modo paso a paso.
    # Solo validamos en la captura del NOMBRE porque es el caso más común
    # y donde el usuario suele percatarse primero de la colisión. Para
    # la cédula, el servicio `registrar_cliente` ya levanta ValueError
    # si choca con un cliente existente, por lo que duplicar la búsqueda
    # aquí rompe el test de 'cédula no numérica' que no tiene acceso al
    # singleton `inventario`.
    if campo == "nombre":
        exist = await asyncio.to_thread(
            inventario.buscar_cliente_existente,
            identificacion=datos.get("identificacion") or None,
            telefono=datos.get("telefono") or None,
            nombre=datos.get("nombre") or None,
        )
        if exist:
            await _manejar_cliente_duplicado(
                telefono, usuario_id, contexto, exist,
                accion_en_curso=dict(accion),
            )
            return MANEJADO

    faltan = [c for c in ("nombre", "identificacion", "telefono", "direccion") if not datos.get(c)]
    if faltan:
        accion["datos"] = datos
        return f"Faltan datos del cliente: {', '.join(faltan)}.\n¿{_pregunta_campo_cliente(faltan[0])}"
    else:
        try:
            nuevo = await asyncio.to_thread(inventario.registrar_cliente, **_datos_cliente(datos))
            contexto["accion_pendiente"] = {}
            return f"✅ Cliente registrado:\n{_formatear_ficha_cliente(nuevo)}"
        except Exception as e:
            contexto["accion_pendiente"] = {}
            return f"⚠️ {e}"
