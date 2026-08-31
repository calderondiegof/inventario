"""Handler de conductores: captura en bloque y paso a paso (nombre, ID, telefono, direccion opcional, placa y placa de trailer opcional)."""
import asyncio
import logging
from typing import Any, Dict, Optional

from core.config import inventario
from core.contexto import guardar_contexto
from core.whatsapp import enviar_mensaje_whatsapp
from handlers import MANEJADO
from services.inventario_service import extraer_placas, normalizar_digitos, parsear_bloque_persona
from utils.whatsapp_formatter import _formatear_ficha_conductor

logger = logging.getLogger(__name__)



def _datos_conductor(p: Dict[str, Any]) -> Dict[str, Any]:
    """Mapea el bloque parseado a los parámetros EXPLÍCITOS de registrar_conductor
    (incluye dirección y la placa opcional del remolque)."""
    return {
        "nombre_conductor": p.get("nombre") or "",
        "id_conductor": p.get("identificacion") or None,
        "telefono_conductor": p.get("telefono") or None,
        "direccion_conductor": p.get("direccion") or None,
        "placa_conductor": p.get("placa") or None,
        "placa_trailer_conductor": p.get("placa_trailer") or None,
    }


def _direccion_opcional(texto: str) -> Optional[str]:
    """Sanitiza la respuesta del paso OPCIONAL de dirección:
    '0', 'omitir', 'no', '-' o vacío -> None; texto libre -> la dirección
    completa (calle, ciudad y país) tal como la escribió el usuario."""
    t = (texto or "").strip()
    if not t or t.lower() in {"0", "omitir", "omite", "no", "-", "ninguna", "cancelar direccion"}:
        return None
    return t
async def capturar_bloque_conductor(telefono: str, usuario_id: int, bodega_id: int,
                                    contexto: Dict[str, Any], texto: str) -> None:
    """En bloque (Conductor): parsea; si falta placa u otro dato, pasa a paso a paso."""
    p = parsear_bloque_persona(texto)
    exist = await asyncio.to_thread(inventario.buscar_conductor_existente,
                                    identificacion=p.get("identificacion") or None,
                                    placa=p.get("placa") or None)
    if exist:
        await enviar_mensaje_whatsapp(telefono, f"El conductor ya se encuentra registrado.\n{_formatear_ficha_conductor(exist)}")
        contexto["accion_pendiente"] = {}
        await guardar_contexto(usuario_id, contexto)
        return
    faltan = [c for c in ("nombre", "identificacion", "placa", "telefono") if not p.get(c)]
    if faltan:
        contexto["accion_pendiente"] = {"tipo": "crear_conductor_paso", "datos": dict(p)}
        contexto["campo_esperado"] = "conductor_" + faltan[0]
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, f"Faltan datos del conductor: {', '.join(faltan)}.\n¿{_pregunta_campo_conductor(faltan[0])}")
        return
    try:
        nuevo = await asyncio.to_thread(inventario.registrar_conductor, **_datos_conductor(p))
        contexto["accion_pendiente"] = {}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, f"✅ Conductor registrado:\n{_formatear_ficha_conductor(nuevo)}")
    except ValueError as e:
        contexto["accion_pendiente"] = {}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, str(e))


def _pregunta_campo_conductor(campo: str) -> str:
    return {
        "nombre": "Nombre completo del conductor:",
        "identificacion": "Identificación (CC/NIT):",
        "placa": "Placa / Patente del camión (si trae remolque, agrégala: 'Trailer BBB456'):",
        "telefono": "Teléfono / celular:",
    }.get(campo, f"{campo}:")



async def procesar_flujo_conductor(telefono: str, usuario_id: int, bodega_id: int,
                                  contexto: Dict[str, Any], accion: Dict[str, Any], texto: str):
    """Punto de entrada del flujo de conductores (bloque y paso a paso)."""
    if accion["tipo"] == "crear_conductor_bloque":
        await capturar_bloque_conductor(telefono, usuario_id, bodega_id, contexto, texto)
        return MANEJADO
    # accion["tipo"] == "crear_conductor_paso"
    # Paso a paso de Conductor: se completa el dato faltante.
    # La DIRECCIÓN es OPCIONAL: si viene del bloque se conserva; si
    # no, se pregunta UNA vez al final (texto libre con ciudad/país,
    # '0'/'omitir' para dejarla en None) y se persiste en la columna
    # exacta 'direccion' de la tabla conductores.
    datos = dict(accion.get("datos") or {})
    esperando_dir = datos.pop("_esperando_direccion", False)
    datos.pop("_dir_preguntada", None)
    if esperando_dir:
        # Respuesta al paso opcional: texto libre, '0'/'omitir' -> None.
        datos["direccion"] = _direccion_opcional(texto)
        faltan = [c for c in ("nombre", "identificacion", "placa", "telefono") if not datos.get(c)]
        if faltan:
            accion["datos"] = datos
            return (f"Faltan datos del conductor: {', '.join(faltan)}.\n"
                               f"¿{_pregunta_campo_conductor(faltan[0])}")
        else:
            accion["datos"] = datos
    else:
        faltan = [c for c in ("nombre", "identificacion", "placa", "telefono") if not datos.get(c)]
        campo = faltan[0] if faltan else None
        if campo == "nombre":
            datos["nombre"] = texto
        elif campo == "identificacion":
            datos["identificacion"] = normalizar_digitos(texto)
        elif campo == "placa":
            # Doble soporte: 1 o 2 placas/patentes ('AAA123 / BBB456'
            # o 'Placa: AAA123, Trailer: BBB456'); el remolque es opcional.
            p1, p2 = extraer_placas(texto)
            datos["placa"] = p1 or texto.strip().upper()
            if p2:
                datos["placa_trailer"] = p2
        elif campo == "telefono":
            datos["telefono"] = texto.strip()
        faltan = [c for c in ("nombre", "identificacion", "placa", "telefono") if not datos.get(c)]
        if faltan:
            accion["datos"] = datos
            return (f"Faltan datos del conductor: {', '.join(faltan)}.\n"
                               f"¿{_pregunta_campo_conductor(faltan[0])}")
        elif not datos.get("direccion"):
            # Obligatorios completos: paso OPCIONAL de dirección.
            datos["_esperando_direccion"] = True
            accion["datos"] = datos
            return ("📍 Dirección del conductor (opcional; puede incluir "
                               "ciudad y país).\nEscribe '0' para omitir:")
        else:
            accion["datos"] = datos
    if not accion.get("datos", {}).get("_esperando_direccion") and not faltan:
        try:
            nuevo = await asyncio.to_thread(inventario.registrar_conductor, **_datos_conductor(datos))
            contexto["accion_pendiente"] = {}
            return f"✅ Conductor registrado:\n{_formatear_ficha_conductor(nuevo)}"
        except Exception as e:
            contexto["accion_pendiente"] = {}
            return f"⚠️ {e}"
