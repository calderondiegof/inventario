"""Handler de materiales: menu de creacion, alta paso a paso y recarga del catalogo global."""
import asyncio
import logging
from typing import Any, Dict, Optional

from core.config import inventario
from core.contexto import guardar_contexto
from core.whatsapp import enviar_botones_whatsapp, enviar_mensaje_whatsapp
from handlers import MANEJADO

logger = logging.getLogger(__name__)



async def iniciar_creacion(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Menú principal del módulo unificado de creación:
    1. Cliente | 2. Conductor | 3. Producto/Material."""
    contexto["accion_pendiente"] = {"tipo": "crear_menu"}
    await guardar_contexto(usuario_id, contexto)
    await enviar_botones_whatsapp(
        telefono,
        "¿Qué deseas crear?",
        [("crear_cliente", "1. Cliente"), ("crear_conductor", "2. Conductor"),
         ("crear_material", "3. Producto / Material")],
    )
async def iniciar_creacion_material(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Flujo paso a paso de Producto/Material: pide nombre y tipo."""
    contexto["accion_pendiente"] = {"tipo": "crear_material_paso", "datos": {}}
    contexto["campo_esperado"] = "material_nombre"
    await guardar_contexto(usuario_id, contexto)
    await enviar_mensaje_whatsapp(telefono, "Nombre del material:")
    await enviar_botones_whatsapp(
        telefono, "¿Es comercializable?",
        [("si", "✅ Sí"), ("no", "❌ No")],
    )



async def procesar_flujo_material(telefono: str, usuario_id: int, bodega_id: int,
                                 contexto: Dict[str, Any], accion: Dict[str, Any], texto: str):
    """Paso a paso de Producto/Material: nombre -> tipo -> comercializable -> confirmar."""
    # Paso a paso de Producto/Material: nombre -> tipo -> comercializable -> confirmar.
    datos = dict(accion.get("datos") or {})
    if not datos.get("nombre"):
        datos["nombre"] = texto
        accion["datos"] = datos
        return ("Tipo del material (BRUTO/SEMILIMPIO/LIMPIO/MERMA):\n"
                           "1. BRUTO  2. SEMILIMPIO  3. LIMPIO  4. MERMA")
    elif not datos.get("tipo_material"):
        tipo_map = {"1": "BRUTO", "2": "SEMILIMPIO", "3": "LIMPIO", "4": "MERMA"}
        datos["tipo_material"] = tipo_map.get(texto.strip(), texto.strip().upper())
        accion["datos"] = datos
        return "¿El material es comercializable? (si/no)"
    elif "es_comercializable" not in datos:
        datos["es_comercializable"] = texto.strip().lower() in {"si", "sí", "yes", "1", "true"}
        accion["datos"] = datos
        return (f"Confirma la creación del material:\n"
                           f"• Nombre: {datos['nombre']}\n"
                           f"• Tipo: {datos['tipo_material']}\n"
                           f"• Comercializable: {'Sí' if datos['es_comercializable'] else 'No'}\n\n"
                           f"Responde *SI* para guardar o *NO* para cancelar.")
    else:
        if texto.strip().lower() in {"si", "sí", "s", "yes", "confirmar", "ok"}:
            try:
                nuevo = await asyncio.to_thread(inventario.registrar_material, **datos)
                contexto["accion_pendiente"] = {}
                return f"✅ Material '{nuevo['nombre']}' registrado y catálogo actualizado."
            except Exception as e:
                contexto["accion_pendiente"] = {}
                return f"⚠️ {e}"
        else:
            contexto["accion_pendiente"] = {}
            return "Creación del material cancelada."
