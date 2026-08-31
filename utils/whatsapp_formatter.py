"""Formateo de mensajes de texto para la API de WhatsApp.

Las funciones públicas se usan desde los handlers:
- ``formatear_movimientos_material``: historial de un material
- ``_formatear_ficha_cliente`` / ``_formatear_ficha_conductor``: fichas
- ``construir_mensaje_seleccion``: confirmación de una selección de revuelto
"""
from typing import Any, Dict, List, Optional


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


def construir_mensaje_seleccion(
    resultado: Dict[str, Any],
    fecha: str,
    materiales_omitidos: Optional[List[str]] = None,
) -> str:
    """Construye el mensaje de confirmación de una selección de Revuelto.

    Plantilla ESTRICTA (validada por ``test_mensaje_seleccion_revuelto`` y
    ``test_caso_produccion_basura_merma_y_omitidos``):

    - Con omitidos::

        ✅ Selección registrada: {N} resultado(s), merma {merma:.2f} kg,
        revuelto: -{revuelto:.0f} kg, fecha {fecha}.

        ⚠️ **Atención:** Los siguientes ítems no se pudieron registrar
        y fueron ignorados:
        - {item} kg (Material no encontrado en el catálogo)

    - Sin omitidos: solo la primera línea, sin sección de alerta.

    Parámetros:
        resultado: dict devuelto por
            ``InventarioServiceConValidacion.registrar_seleccion_revuelto``
            con claves ``registros``, ``merma_kg`` y ``revuelto_descontado``.
        fecha: fecha de la operación (string ISO o legible).
        materiales_omitidos: lista de strings con formato
            ``"<nombre> <cantidad>"`` que no se pudieron registrar.
    """
    registros = resultado.get("registros") or []
    # El último registro corresponde a la salida del Revuelto (negativo).
    # Los anteriores son los resultados vendibles.
    num_resultados = max(len(registros) - 1, 0)
    merma = float(resultado.get("merma_kg") or 0)
    revuelto = float(resultado.get("revuelto_descontado") or 0)

    msg = (
        f"✅ Selección registrada: {num_resultados} resultado(s), "
        f"merma {merma:.2f} kg, revuelto: -{revuelto:.0f} kg, "
        f"fecha {fecha}."
    )

    omitidos = [str(o).strip() for o in (materiales_omitidos or []) if str(o).strip()]
    if omitidos:
        detalle = "\n".join(
            f"- {o} kg (Material no encontrado en el catálogo)" for o in omitidos
        )
        msg += (
            "\n\n⚠️ **Atención:** Los siguientes ítems no se pudieron "
            f"registrar y fueron ignorados:\n{detalle}"
        )
    return msg

