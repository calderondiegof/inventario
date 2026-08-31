"""Handler de comando PDF: wizard de modo + reimpresion dinamica + listado."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from core import config as _config
from core.contexto import guardar_contexto
from core.whatsapp import (
    enviar_documento_whatsapp, enviar_lista_whatsapp, enviar_mensaje_whatsapp,
)
from services.pdf_remision_service import (
    MAPA_MODOS_IMPRESION, ModoImpresion, PdfRemisionService,
    PdfRemisionError, RemisionPdf, construir_filas_listado_pdf,
)

logger = logging.getLogger(__name__)

_service: Optional[PdfRemisionService] = None
TIPO_SELECCION_PDF = "seleccion_pdf"
TIPO_MODO_IMPRESION = "esperando_modo_impresion_pdf"


def _get_service() -> PdfRemisionService:
    global _service
    if _service is None:
        _service = PdfRemisionService(
            supabase=_config.supabase,
            http_client=getattr(_config, "http_client", None),
        )
    return _service


def _texto_opciones_modo() -> str:
    return (
        "1️⃣  Moneda local ($ COP)\n"
        "2️⃣  Dolares ($ USD)\n"
        "3️⃣  Ambas monedas ($ COP y $ USD)\n"
        "4️⃣  Sin valores (despacho / bodega)"
    )


def _pregunta_modo(numero: str) -> str:
    return (
        f"Como deseas que se impriman los valores en el PDF de la "
        f"Remision {numero}?\n\n{_texto_opciones_modo()}"
    )
