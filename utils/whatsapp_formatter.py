"""Formateo de mensajes de texto para la API de WhatsApp.

Las funciones públicas se usan desde los handlers. La plantilla de selección
vive en InventarioServiceConValidacion.construir_mensaje_seleccion.
"""
from typing import Any, Dict


def formatear_movimientos_material(resultado: Dict[str, Any]) -> str:
    lineas = [f"📑 Movimientos de {resultado['material']}", ""]
    saldo_inicial = resultado.get("saldo_inicial")
    if saldo_inicial:
        lineas.append(f"Saldo inicial (antes del rango): {saldo_inicial:,.2f} kg")
        lineas.append("")
    movimientos = resultado.get("movimientos", [])
    if not movimientos:
        lineas.append("No hay movimientos en ese rango.")
    else:
        for mv in movimientos:
            signo = "+" if mv["cantidad_kg"] >= 0 else ""
            fuente = f" ({mv['fuente']})" if mv.get("fuente") else ""
            lineas.append(
                f"• {mv['fecha']} — {mv['tipo']}: {signo}{mv['cantidad_kg']:,.2f} kg{fuente} "
                f"| saldo: {mv['saldo_acumulado']:,.2f} kg"
            )
    return "\n".join(lineas)
def _formatear_ficha_cliente(p: Dict[str, Any]) -> str:
    return (
        f"👤 *Cliente:* {p.get('nombre','')}\n"
        f"🪪 ID: {p.get('identificacion','')}\n"
        f"📱 Tel: {p.get('telefono','')}\n"
        f"📍 Dirección: {p.get('direccion','')}"
    ) if p.get("nombre") else ""

def _formatear_ficha_conductor(p: Dict[str, Any]) -> str:
    """Ficha del conductor con nomenclatura dual Placa / Patente; muestra la
    patente del remolque (placa_trailer / placa_trailer_conductor) si existe."""
    trailer = p.get("placa_trailer") or p.get("placa_trailer_conductor") or ""
    linea_trailer = f"\n🚛 Trailer/Patente remolque: {trailer}" if trailer else ""
    direccion = p.get("direccion") or p.get("direccion_conductor") or ""
    linea_direccion = f"\n📍 Dirección: {direccion}" if direccion else ""
    return (
        f"🚚 *Conductor:* {p.get('nombre','')}\n"
        f"🪪 ID: {p.get('identificacion','')}\n"
        f"🚗 Placa / Patente: {p.get('placa','') or p.get('placa_conductor','')}"
        f"{linea_trailer}{linea_direccion}\n"
        f"📱 Tel: {p.get('telefono','') or p.get('telefono_conductor','')}"
    ) if p.get("nombre") or p.get("nombre_conductor") else ""

