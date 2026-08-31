"""Handler de comando PDF: wizard de modo + reimpresion dinamica + listado."""
from __future__ import annotations

import logging
from typing import List, Optional

from core import config as _config
from core.contexto import guardar_contexto, obtener_contexto
from core.whatsapp import (
    enviar_documento_whatsapp, enviar_lista_whatsapp, enviar_mensaje_whatsapp,
)
from services.pdf_remision_service import (
    MAPA_MODOS_IMPRESION, ModoImpresion, PdfRemisionService,
    PdfRemisionError, construir_filas_listado_pdf,
)

logger = logging.getLogger(__name__)

_service: Optional[PdfRemisionService] = None
TIPO_MODO_IMPRESION = "esperando_modo_impresion_pdf"


def _get_service() -> PdfRemisionService:
    global _service
    if _service is None:
        _service = PdfRemisionService(supabase=_config.supabase)
    return _service


_OPCIONES_MODO: List[dict] = [
    {"id": "modo_1", "title": "Moneda local ($ COP)"},
    {"id": "modo_2", "title": "Dolares ($ USD)"},
    {"id": "modo_3", "title": "Ambas monedas ($ COP y $ USD)"},
    {"id": "modo_4", "title": "Sin valores (despacho / bodega)"},
]


def _texto_opciones_modo() -> str:
    return (
        "1  Moneda local ($ COP)\n"
        "2  Dolares ($ USD)\n"
        "3  Ambas monedas ($ COP y $ USD)\n"
        "4  Sin valores (despacho / bodega)"
    )


def _pregunta_modo(numero: str) -> str:
    return (
        f"Como deseas que se impriman los valores en el PDF de la "
        f"Remision {numero}?\n\n{_texto_opciones_modo()}"
    )


async def manejar_comando_pdf(
    texto: str, telefono: str, usuario_id: str,
) -> str:
    partes = texto.strip().split()
    if len(partes) < 2:
        return await _listar_remisiones(telefono, usuario_id)
    numero_raw = partes[1]
    modo_raw = partes[2] if len(partes) > 2 else None
    if modo_raw:
        return await _procesar_directo(telefono, usuario_id, numero_raw, modo_raw)
    await guardar_contexto(
        usuario_id,
        {"tipo": TIPO_MODO_IMPRESION, "numero_remision": numero_raw},
    )
    await enviar_lista_whatsapp(
        destino=telefono,
        header_title="Modo de impresion", header_subtitle=numero_raw,
        body_text=_pregunta_modo(numero_raw),
        boton_principal="Seleccionar modo", opciones=_OPCIONES_MODO,
    )
    return ""


async def _procesar_directo(
    telefono: str, usuario_id: str, numero_raw: str, modo_raw: str,
) -> str:
    service = _get_service()
    try:
        ruta, modo, advertencia = await service.reimprimir_pdf_dinamico(
            numero_remision=numero_raw, modo_solicitado=modo_raw,
        )
        if advertencia:
            await enviar_mensaje_whatsapp(telefono, advertencia)
        await enviar_documento_whatsapp(
            destino=telefono, ruta_archivo=str(ruta),
            nombre_documento=f"{numero_raw}_{modo.value}.pdf",
        )
        await guardar_contexto(usuario_id, {})
        return f"Remision {numero_raw} enviada en modo '{modo.value}'."
    except PdfRemisionError as exc:
        logger.error("Error PDF directo %s: %s", numero_raw, exc)
        return f"Error: {exc}"
    except Exception as exc:
        logger.exception("Error inesperado PDF directo %s", numero_raw)
        return f"Error generando PDF: {exc}"


async def manejar_respuesta_modo_impresion(
    telefono: str, texto_normalizado: str, usuario_id: str,
) -> str:
    contexto = obtener_contexto(usuario_id) or {}
    numero_remision = contexto.get("numero_remision", "")
    modo_raw = texto_normalizado.strip()
    for prefijo in ("modo_", "opcion_"):
        if modo_raw.startswith(prefijo):
            modo_raw = modo_raw[len(prefijo):]
            break
    if modo_raw not in MAPA_MODOS_IMPRESION:
        primera = texto_normalizado.strip().split()[0] if texto_normalizado.strip() else ""
        if primera in MAPA_MODOS_IMPRESION:
            modo_raw = primera
    service = _get_service()
    try:
        ruta, modo, advertencia = await service.reimprimir_pdf_dinamico(
            numero_remision=numero_remision, modo_solicitado=modo_raw,
        )
        if advertencia:
            await enviar_mensaje_whatsapp(telefono, advertencia)
        await enviar_documento_whatsapp(
            destino=telefono, ruta_archivo=str(ruta),
            nombre_documento=f"{numero_remision}_{modo.value}.pdf",
        )
        await guardar_contexto(usuario_id, {})
        return f"Remision {numero_remision} enviada en modo '{modo.value}'."
    except PdfRemisionError as exc:
        logger.error("Error wizard PDF %s: %s", numero_remision, exc)
        await guardar_contexto(usuario_id, {})
        return f"Error: {exc}"
    except Exception as exc:
        logger.exception("Error inesperado wizard PDF %s", numero_remision)
        await guardar_contexto(usuario_id, {})
        return f"Error generando PDF: {exc}"


async def _listar_remisiones(telefono: str, usuario_id: str) -> str:
    service = _get_service()
    remisiones = service.listar_remisiones_con_pdf(limite=10)
    if not remisiones:
        return "No hay remisiones recientes con PDF."
    filas = construir_filas_listado_pdf(remisiones)
    await enviar_lista_whatsapp(
        destino=telefono,
        header_title="Remisiones", header_subtitle="Ultimas remisiones",
        body_text="Selecciona una remision para reimprimir:",
        boton_principal="Ver remisiones",
        opciones=[{"title": titulo, "description": desc} for _, titulo, desc in filas],
    )
    return ""
