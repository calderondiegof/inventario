"""Handler de consultas: reportes diarios, graficos, inventario total y movimientos."""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from core.config import inventario
from core.contexto import guardar_contexto
from core.whatsapp import (
    enviar_botones_whatsapp, enviar_imagen_whatsapp, enviar_lista_whatsapp,
    enviar_mensaje_whatsapp,
)
from reporte_grafico import generar_y_subir_grafico_stock

logger = logging.getLogger(__name__)



async def enviar_reporte_diario(telefono: str, bodega_id: int, message: Dict[str, Any],
                                dias_atras: int = 0, fecha: Optional[str] = None) -> None:
    """Envía el reporte diario de la bodega. dias_atras=0 → 'hoy', 1 → 'ayer'.
    Si se pasa `fecha` (ISO) se usa esa fecha explícita (flujo Reporte por fecha)."""
    if not fecha:
        fecha = fecha_local_mensaje(message)
        if dias_atras:
            fecha = (datetime.fromisoformat(fecha).date() - timedelta(days=dias_atras)).isoformat()
    reporte = await asyncio.to_thread(inventario.obtener_reporte_diario_texto, bodega_id, fecha)
    await enviar_mensaje_whatsapp(telefono, reporte)


async def enviar_grafico_inventario(telefono: str, bodega_id: int) -> None:
    """Genera y envía la gráfica de stock de la bodega (reporte_grafico)."""
    url = await asyncio.to_thread(generar_y_subir_grafico_stock, bodega_id)
    if url:
        await enviar_imagen_whatsapp(telefono, url, f"Inventario de la bodega {bodega_id}")
    else:
        await enviar_mensaje_whatsapp(telefono, "No hay datos para generar el gráfico.")


async def iniciar_inventario_total(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Submenú de 'Inventario Total': botones para elegir informe en texto o gráfico."""
    contexto["borrador_pendiente"] = {}
    contexto["campo_esperado"] = None
    contexto["accion_pendiente"] = {}
    await guardar_contexto(usuario_id, contexto)
    await enviar_botones_whatsapp(
        telefono, "¿Cómo deseas ver el Inventario Total?",
        [("inv_txt", "Ver informe texto"), ("inv_graf", "Ver informe Grafico")],
    )


async def iniciar_reporte_por_fecha(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Flujo 'Reporte de Hoy' con fecha opcional: pide la fecha (DD/MM/AAAA)
    y ofrece el botón rápido 'Hoy'."""
    contexto["borrador_pendiente"] = {}
    contexto["campo_esperado"] = None
    contexto["accion_pendiente"] = {"tipo": "reporte_fecha"}
    await guardar_contexto(usuario_id, contexto)
    await enviar_botones_whatsapp(
        telefono,
        "Por favor, ingresa la fecha del reporte que deseas consultar "
        "(formato DD/MM/AAAA) o presiona el botón para consultar hoy.",
        [("inv_hoy_hoy", "📅 Hoy")],
    )


async def enviar_inventario_total(telefono: str, bodega_id: int) -> None:
    """Envía el resumen con todos los saldos de la bodega, ordenados
    alfabéticamente (obtener_saldos_bodega ya devuelve la lista ordenada)."""
    saldos = await asyncio.to_thread(inventario.obtener_saldos_bodega, bodega_id)
    if not saldos:
        await enviar_mensaje_whatsapp(telefono, f"No hay stock registrado en la Bodega #{bodega_id}.")
        return
    total_kg = sum(x["saldo_kg"] for x in saldos)
    lineas = [f"📦 Inventario actual — Bodega #{bodega_id}", ""]
    lineas += [f"• {x['material']}: {x['saldo_kg']:,.2f} kg" for x in saldos]
    lineas += ["", f"*Total inventario: {total_kg:,.2f} kg*"]
    await enviar_mensaje_whatsapp(telefono, "\n".join(lineas))


async def pedir_movimientos_material(telefono: str, usuario_id: int, contexto: Dict[str, Any]) -> None:
    """Activa el flujo interactivo para consultar movimientos de un material.
    Envía un List Message con TODOS los materiales del catálogo activo; la
    selección (button/list reply llega como texto = nombre exacto) reengancha
    con el estado `movimientos_material`, que resuelve por coincidencia
    exacta. Fallback: si no hay catálogo, pregunta por texto."""
    contexto["accion_pendiente"] = {"tipo": "movimientos_material"}
    contexto["borrador_pendiente"] = {}
    contexto["campo_esperado"] = None
    await guardar_contexto(usuario_id, contexto)
    materiales = sorted(inventario.catalogo_materiales.values(), key=lambda m: m.nombre)
    if materiales:
        await enviar_lista_whatsapp(
            telefono,
            "Selecciona el material para ver sus movimientos:",
            "Ver materiales",
            [(m.nombre, m.nombre, m.tipo_material) for m in materiales],
            titulo_lista="Materiales",
        )
    else:
        await enviar_mensaje_whatsapp(telefono, "¿De qué material deseas ver los movimientos?")

