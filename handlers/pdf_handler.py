"""Handler de comando PDF: búsqueda, listado y envío de remisiones por WhatsApp."""

import logging
from typing import Optional

from core import config as _config
from core.contexto import guardar_contexto
from core.whatsapp import enviar_lista_whatsapp, enviar_mensaje_whatsapp
from services.pdf_remision_service import (
    PdfRemisionService,
    construir_filas_listado_pdf,
)

logger = logging.getLogger(__name__)

# Singleton del servicio (lazy).
_service: Optional[PdfRemisionService] = None

# Tipo que se graba en accion_pendiente para identificar respuesta de lista PDF.
TIPO_SELECCION_PDF = "seleccion_pdf"


def _get_service() -> PdfRemisionService:
    """Acceso lazy al PdfRemisionService (usa el supabase de config)."""
    global _service
    if _service is None:
        _service = PdfRemisionService(supabase=_config.supabase)
    return _service


async def manejar_comando_pdf(
    texto: str, telefono: str, usuario_id: str = ""
) -> str:
    """Procesa solicitudes de envío de PDF de remisión.

    Comandos soportados:
      pdf <numero>  -> envía el PDF de esa remisión específica.
      pdf            -> lista las últimas remisiones con PDF para selección.
    """
    partes = texto.strip().split()
    if len(partes) > 1:
        return await _enviar_por_numero(telefono, partes[1])
    return await _listar_para_seleccion(telefono, usuario_id)


async def _enviar_por_numero(telefono: str, numero: str) -> str:
    """Busca la remisión por número y envía el PDF. Retorna mensaje."""
    ok, mensaje = await _get_service().enviar_pdf_remision(
        telefono=telefono,
        numero_remision=numero,
    )
    if ok:
        logger.info("PDF de %s enviado a %s", numero, telefono)
    else:
        logger.warning("No se pudo enviar PDF %s: %s", numero, mensaje)
    return mensaje


async def _listar_para_seleccion(telefono: str, usuario_id: str = "") -> str:
    """Envía una lista interactiva con las últimas remisiones con PDF.

    Si ``usuario_id`` se proporciona, guarda ``accion_pendiente`` en el
    contexto para que el router sepa que la siguiente respuesta interactiva
    pertenece a este flujo.
    """
    remisiones = _get_service().listar_remisiones_con_pdf(limite=10)

    if not remisiones:
        return (
            "No hay remisiones con PDF disponibles para listar. "
            "Usa 'pdf <numero>' para buscar una específica."
        )

    filas = construir_filas_listado_pdf(remisiones)

    if len(filas) <= 10:
        await enviar_lista_whatsapp(
            destino=telefono,
            texto="Selecciona una remisión para recibir su PDF:",
            titulo_boton="Ver PDF",
            filas=filas,
            titulo_lista="Remisiones con PDF",
        )
    else:
        lineas = ["Remisiones con PDF:"]
        for r in remisiones[:20]:
            fecha = r.fecha_creacion[:10] if r.fecha_creacion else "-"
            lineas.append(
                f"  - {r.numero_remision}  |  "
                f"{(r.cliente or 'Sin cliente'):<30}  |  {fecha}"
            )
        lineas.append("")
        lineas.append("Usa 'pdf <numero>' para enviar el PDF.")
        await enviar_mensaje_whatsapp(telefono, "\n".join(lineas))

    if usuario_id:
        await guardar_contexto(
            usuario_id,
            {"accion_pendiente": {"tipo": TIPO_SELECCION_PDF}},
        )
    return ""


async def manejar_respuesta_seleccion_pdf(telefono: str, respuesta: str) -> str:
    """Maneja la selección del usuario desde la lista interactiva.

    WhatsApp entrega ``interactive.list_reply.id``, que en nuestro diseño
    ES el número de remisión (ej. "REM_117").
    """
    respuesta = (respuesta or "").strip()
    if not respuesta:
        return "Seleccion vacia. Usa 'pdf' para ver el listado de nuevo."
    return await _enviar_por_numero(telefono, respuesta)
