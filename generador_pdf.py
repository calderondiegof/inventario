"""Módulo para generar PDFs de remisión de material"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.pdfgen import canvas


RUTA_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo_ferroma.jpeg")

ANCHO, ALTO = letter  # 612 x 792 pt

MARGEN_IZQ = 40
MARGEN_DER = 40
COLUMNAS_TABLA = [
    ("MATERIAL", 150),
    ("CANTIDAD KG", 75),
    ("N° EMPAQUE", 70),
    ("VR X KILO", 65),
    ("VR TOTAL PESOS", 80),
    ("VR KILO DÓLAR", 65),
    ("VR TOTAL DÓLAR", 75),
]
ANCHO_TABLA = sum(w for _, w in COLUMNAS_TABLA)
FILAS_TABLA = 10
ALTO_FILA = 16


def _texto(c: canvas.Canvas, x: float, y: float, texto: str, tam: int = 9, negrita: bool = False, centrado: bool = False, ancho_centro: float = 0) -> None:
    c.setFont("Helvetica-Bold" if negrita else "Helvetica", tam)
    if centrado:
        c.drawCentredString(x + ancho_centro / 2, y, texto)
    else:
        c.drawString(x, y, texto)


def _campo_con_linea(c: canvas.Canvas, label: str, valor: Optional[str], x: float, y: float, x_linea: float, x_fin_linea: float) -> None:
    _texto(c, x, y, label, tam=10, negrita=True)
    c.setLineWidth(0.7)
    c.line(x_linea, y - 2, x_fin_linea, y - 2)
    if valor:
        _texto(c, x_linea + 3, y + 1, str(valor), tam=9.5)


def _encabezado(c: canvas.Canvas, *, fecha: str, cliente: str, documento: Optional[str], direccion: Optional[str],
                 celular: Optional[str], numero_remision: str) -> float:
    y = ALTO - 45

    _campo_con_linea(c, "Cliente:", cliente, MARGEN_IZQ, y, MARGEN_IZQ + 60, 330)
    y -= 30
    _campo_con_linea(c, "Id Cliente:", documento, MARGEN_IZQ, y, MARGEN_IZQ + 70, 330)
    y -= 30
    _campo_con_linea(c, "Celular:", celular, MARGEN_IZQ, y, MARGEN_IZQ + 60, 330)
    y -= 30
    _campo_con_linea(c, "Dirección:", direccion, MARGEN_IZQ, y, MARGEN_IZQ + 70, 330)

    x_titulo = 355
    y_titulo = ALTO - 45
    _texto(c, x_titulo, y_titulo, "Remision Salida", tam=11, negrita=True)
    _texto(c, x_titulo + 15, y_titulo - 16, "de Material", tam=11, negrita=True)

    _texto(c, x_titulo + 25, y_titulo - 55, "N°", tam=10, negrita=True)
    c.setLineWidth(0.7)
    c.line(x_titulo + 55, y_titulo - 62, x_titulo + 130, y_titulo - 62)
    _texto(c, x_titulo + 60, y_titulo - 59, str(numero_remision), tam=9.5)

    _texto(c, x_titulo, y_titulo - 90, f"Fecha: {fecha}", tam=9)

    # Logo (caja negra con el isotipo, igual al membrete impreso)
    ancho_logo, alto_logo = 145, 100
    x_logo = ANCHO - MARGEN_DER - ancho_logo
    y_logo = ALTO - 45 - alto_logo + 15
    if os.path.exists(RUTA_LOGO):
        c.drawImage(RUTA_LOGO, x_logo, y_logo, width=ancho_logo, height=alto_logo,
                     preserveAspectRatio=True, anchor='c', mask='auto')

    return ALTO - 45 - 4 * 30 - 25  # y donde continúa el resto del documento


def _tabla_materiales(c: canvas.Canvas, y_inicio: float, items: List[Dict[str, Any]]) -> float:
    x = MARGEN_IZQ
    y = y_inicio

    # Encabezado de tabla
    c.setLineWidth(0.7)
    c.rect(x, y - ALTO_FILA, ANCHO_TABLA, ALTO_FILA)
    cx = x
    for nombre_col, ancho_col in COLUMNAS_TABLA:
        c.rect(cx, y - ALTO_FILA, ancho_col, ALTO_FILA)
        _texto(c, cx, y - ALTO_FILA + 5, nombre_col, tam=7.5, negrita=True, centrado=True, ancho_centro=ancho_col)
        cx += ancho_col
    y -= ALTO_FILA

    total_kg = 0.0
    total_pesos = 0.0
    total_dolar = 0.0

    for i in range(FILAS_TABLA):
        cx = x
        item = items[i] if i < len(items) else None
        for nombre_col, ancho_col in COLUMNAS_TABLA:
            c.rect(cx, y - ALTO_FILA, ancho_col, ALTO_FILA)
            if item:
                valor = ""
                if nombre_col == "MATERIAL":
                    valor = str(item.get("material_nombre", ""))
                elif nombre_col == "CANTIDAD KG":
                    cantidad = float(item.get("cantidad_kg", 0) or 0)
                    total_kg += cantidad
                    valor = f"{cantidad:,.2f}"
                elif nombre_col == "VR X KILO":
                    precio = float(item.get("precio_unitario", 0) or 0)
                    if precio:
                        valor = f"{precio:,.0f}"
                elif nombre_col == "VR TOTAL PESOS":
                    cantidad = float(item.get("cantidad_kg", 0) or 0)
                    precio = float(item.get("precio_unitario", 0) or 0)
                    subtotal = cantidad * precio
                    if subtotal:
                        total_pesos += subtotal
                        valor = f"{subtotal:,.0f}"
                if valor:
                    _texto(c, cx + 3, y - ALTO_FILA + 5, valor, tam=8)
            cx += ancho_col
        y -= ALTO_FILA

    # Fila de totales
    cx = x
    for idx, (nombre_col, ancho_col) in enumerate(COLUMNAS_TABLA):
        c.rect(cx, y - ALTO_FILA, ancho_col, ALTO_FILA)
        if idx == 0:
            _texto(c, cx + 3, y - ALTO_FILA + 5, "TOTALES", tam=8, negrita=True)
        elif nombre_col == "CANTIDAD KG":
            _texto(c, cx + 3, y - ALTO_FILA + 5, f"{total_kg:,.2f}", tam=8, negrita=True)
        elif nombre_col == "VR TOTAL PESOS":
            _texto(c, cx + 3, y - ALTO_FILA + 5, f"{total_pesos:,.0f}", tam=8, negrita=True)
        cx += ancho_col
    y -= ALTO_FILA

    return y


def _observaciones(c: canvas.Canvas, y: float) -> float:
    y -= 25
    _texto(c, MARGEN_IZQ, y, "OBSERVACIONES:", tam=9.5, negrita=True)
    c.setLineWidth(0.5)
    for _ in range(2):
        y -= 20
        c.line(MARGEN_IZQ + 90, y - 2, ANCHO - MARGEN_DER, y - 2)
    return y


def _datos_conductor(c: canvas.Canvas, y: float, *, conductor: Optional[str], id_conductor: Optional[str],
                       celular_conductor: Optional[str], placa: Optional[str]) -> float:
    y -= 45
    _campo_con_linea(c, "CONDUCTOR:", conductor, MARGEN_IZQ + 20, y, MARGEN_IZQ + 100, MARGEN_IZQ + 320)
    y -= 30
    _campo_con_linea(c, "ID CONDUCTOR:", id_conductor, MARGEN_IZQ + 20, y, MARGEN_IZQ + 110, MARGEN_IZQ + 320)
    y -= 30
    _campo_con_linea(c, "CELULAR:", celular_conductor, MARGEN_IZQ + 20, y, MARGEN_IZQ + 90, MARGEN_IZQ + 320)
    y -= 30
    _texto(c, MARGEN_IZQ + 5, y, "PATENTE O PLACA", tam=9.5, negrita=True)
    _texto(c, MARGEN_IZQ + 30, y - 12, "VEHICULO", tam=9.5, negrita=True)
    c.setLineWidth(0.7)
    c.line(MARGEN_IZQ + 100, y - 12, MARGEN_IZQ + 320, y - 12)
    if placa:
        _texto(c, MARGEN_IZQ + 103, y - 9, str(placa), tam=9.5)
    return y - 40


def _firmas(c: canvas.Canvas) -> None:
    y = 90
    c.setLineWidth(0.7)
    c.line(MARGEN_IZQ, y + 15, MARGEN_IZQ + 260, y + 15)
    _texto(c, MARGEN_IZQ, y, "NOMBRE Y FIRMA DESPACHADOR FERROMA", tam=8.5)
    _texto(c, MARGEN_IZQ, y - 14, "ID:", tam=8.5)

    x_der = 340
    c.line(x_der, y + 15, x_der + 230, y + 15)
    _texto(c, x_der, y, "NOMBRE Y FIRMA CONDUCTOR", tam=8.5)
    _texto(c, x_der, y - 14, "ID:", tam=8.5)


def generar_remision_pdf_archivo(
    ruta_salida: str,
    *,
    fecha: str,
    cliente: str,
    documento: Optional[str] = None,
    direccion: Optional[str] = None,
    celular: Optional[str] = None,
    placa: Optional[str] = None,
    conductor: Optional[str] = None,
    id_conductor: Optional[str] = None,
    celular_conductor: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    numero_remision: str = "SIN-NUMERO",
    bodega_id: Optional[int] = None,
) -> str:
    """
    Genera la remisión de salida de material de FERROMA en formato PDF,
    replicando el modelo REM_MODELO.pdf, y la guarda en `ruta_salida`.
    """
    items = items or []

    c = canvas.Canvas(ruta_salida, pagesize=letter)
    c.setTitle(f"Remision {numero_remision}")

    y = _encabezado(
        c, fecha=fecha, cliente=cliente, documento=documento,
        direccion=direccion, celular=celular, numero_remision=numero_remision,
    )
    y = _tabla_materiales(c, y, items)
    y = _observaciones(c, y)
    _datos_conductor(
        c, y, conductor=conductor, id_conductor=id_conductor,
        celular_conductor=celular_conductor, placa=placa,
    )
    _firmas(c)

    c.showPage()
    c.save()
    return ruta_salida