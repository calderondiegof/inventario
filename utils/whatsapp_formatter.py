"""Formateo de mensajes de texto para la API de WhatsApp."""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)



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
def construir_mensaje_seleccion(r: Dict[str, Any], fecha: str,
                                omitidos: Optional[List[str]] = None) -> str:
    """Plantilla ESTRICTA de la confirmación de selección por WhatsApp.

    Función SÍNCRONA a propósito: no realiza ningún `await`, por lo que se
    llama directamente en el dispatch de `procesar_un_mensaje`. (Si fuera
    `async def`, un llamado sin `await` enviaría un objeto coroutine como
    mensaje en vez del texto real.)

    Formato base:
      ✅ Selección registrada: {n} resultado(s), merma {merma:.2f} kg,
      revuelto: -{total:.0f} kg, fecha {fecha}.

    Si hay ítems que no pudieron registrarse (no encontrados en catálogo),
    se añade la sección de alerta — los ítems omitidos JAMÁS se ignoran en
    silencio."""
    msg = (f"✅ Selección registrada: {len(r['registros']) - 1} resultado(s), "
           f"merma {r['merma_kg']:.2f} kg, revuelto: -{r['revuelto_descontado']:.0f} kg, "
           f"fecha {fecha}.")
    if omitidos:
        detalle = "\n".join(f"- {o} kg (Material no encontrado en el catálogo)"
                            for o in omitidos)
        msg += ("\n\n⚠️ **Atención:** Los siguientes ítems no se pudieron "
                f"registrar y fueron ignorados:\n{detalle}")

