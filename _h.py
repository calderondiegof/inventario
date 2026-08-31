"""Generador del pdf_handler.py (parte 1/2): header + helpers"""
import pathlib
code = r'''"""Handler de comando PDF: wizard de modo + reimpresion dinamica + listado."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from core import config as _config
from core.contexto import guardar_contexto
from core.whatsapp import (
    enviar_botones_whatsapp,
    enviar_documento_whatsapp,
    enviar_lista_whatsapp,
    enviar_mensaje_whatsapp,
)
from services.pdf_remision_service import (
    MAPA_MODOS_IMPRESION,
    ModoImpresion,
    PdfRemisionService,
    PdfRemisionError,
    RemisionPdf,
    construir_filas_listado_pdf,
)

logger = logging.getLogger(__name__)

# Singleton del servicio.
_service: Optional[PdfRemisionService] = None

# Tipos de accion_pendiente.
TIPO_SELECCION_PDF = "seleccion_pdf"
TIPO_MODO_IMPRESION = "esperando_modo_impresion_pdf"

# Etiquetas humanas para cada modo.
MODO_LABELS = {
    ModoImpresion.MONEDA_LOCAL: "1️⃣  Moneda local ($ COP)",
    ModoImpresion.DOLARES:      "2️⃣  Dolares ($ USD)",
    ModoImpresion.AMBAS:        "3️⃣  Ambas monedas ($ COP y $ USD)",
    ModoImpresion.SIN_VALORES:  "4️⃣  Sin valores (despacho / bodega)",
}

MODO_PREGUNTA_TPL = (
    "Como deseas que se impriman los valores en el PDF de la "
    "Remision {numero}?\n\n{opciones}"
)


def _get_service() -> PdfRemisionService:
    global _service
    if _service is None:
        _service = PdfRemisionService(supabase=_config.supabase,
                                      http_client=getattr(_config, "http_client", None))
    return _service


def _opciones_modo_texto() -> str:
    return "\n".join(MODO_LABELS[m] for m in ModoImpresion)


def _mensaje_pregunta_modo(numero: str) -> str:
    return MODO_PREGUNTA_TPL.format(numero=numero, opciones=_opciones_modo_texto())
'''
pathlib.Path("handlers/pdf_handler.py").write_text(code, encoding="utf-8")
print("h1 OK")
