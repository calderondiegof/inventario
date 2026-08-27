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
from reportlab.pdfbase.pdfmetrics import stringWidth


RUTA_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo_ferroma.jpeg")

ANCHO, ALTO = letter  # 612 x 792 pt

MARGEN_IZQ = 33            # x donde inician las etiquetas (modelo REM_MODELO)
X_TABLA = 31               # borde izquierdo de la tabla (modelo)
COLUMNAS_TABLA = [
    ("MATERIAL", 146),
    ("CANTIDAD KG", 65),
    ("N° EMPAQUE", 65),
    ("VR X KILO", 65),
    ("VR TOTAL PESOS", 65),
    ("VR KILO DÓLAR", 65),
    ("VR TOTAL DÓLAR", 73),
]
ANCHO_TABLA = sum(w for _, w in COLUMNAS_TABLA)   # 544 (modelo: x 31 -> 575)

# Orden de Salida (estado='ORDEN_SALIDA'): SIN columnas financieras.
# Mantiene el mismo ancho total del modelo (544) para respetar el marco.
COLUMNAS_TABLA_ORDEN_SALIDA = [
    ("MATERIAL", 350),
    ("CANTIDAD KG", 194),
]
ANCHO_TABLA_ORDEN_SALIDA = sum(w for _, w in COLUMNAS_TABLA_ORDEN_SALIDA)

# Modos de impresión de valores de la Remisión Aprobada (elección del usuario
# de Contabilidad al terminar la captura de precios). Todos los juegos de
# columnas conservan el ancho total del modelo (544) para respetar el marco.
COLUMNAS_TABLA_MONEDA_LOCAL = [
    ("MATERIAL", 200),
    ("CANTIDAD KG", 100),
    ("VR X KILO", 120),
    ("VR TOTAL PESOS", 124),
]
COLUMNAS_TABLA_DOLARES = [
    ("MATERIAL", 200),
    ("CANTIDAD KG", 100),
    ("VR KILO DÓLAR", 120),
    ("VR TOTAL DÓLAR", 124),
]
COLUMNAS_TABLA_AMBAS = [
    ("MATERIAL", 140),
    ("CANTIDAD KG", 80),
    ("VR X KILO", 80),
    ("VR TOTAL PESOS", 84),
    ("VR KILO DÓLAR", 78),
    ("VR TOTAL DÓLAR", 82),
]
ANCHO_TABLA_MODOS = {
    "MONEDA_LOCAL": sum(w for _, w in COLUMNAS_TABLA_MONEDA_LOCAL),
    "DOLARES": sum(w for _, w in COLUMNAS_TABLA_DOLARES),
    "AMBAS": sum(w for _, w in COLUMNAS_TABLA_AMBAS),
    "SIN_VALORES": ANCHO_TABLA_ORDEN_SALIDA,
}
MODOS_VALORES = {"MONEDA_LOCAL", "DOLARES", "AMBAS", "SIN_VALORES"}
FILAS_TABLA = 10
ALTO_FILA = 15.3           # alto de fila del modelo (183.8 / 12 filas)
Y_TABLA = 639              # borde superior de la tabla (modelo: top 153)


def _texto(c: canvas.Canvas, x: float, y: float, texto: str, tam: int = 9, negrita: bool = False, centrado: bool = False, ancho_centro: float = 0) -> None:
    c.setFont("Helvetica-Bold" if negrita else "Helvetica", tam)
    if centrado:
        c.drawCentredString(x + ancho_centro / 2, y, texto)
    else:
        c.drawString(x, y, texto)


def _campo_con_linea(c: canvas.Canvas, label: str, valor: Optional[str], x: float, y: float, x_linea: float, x_fin_linea: float, tam_label: int = 10) -> None:
    """Etiqueta con baseline en (x, y); línea 8pt por debajo; valor sentado sobre la línea."""
    _texto(c, x, y, label, tam=tam_label, negrita=True)
    c.setLineWidth(0.7)
    c.line(x_linea, y - 8, x_fin_linea, y - 8)
    if valor:
        _texto(c, x_linea + 3, y - 6, str(valor), tam=9.5)


def _encabezado(c: canvas.Canvas, *, fecha: str, cliente: str, documento: Optional[str], direccion: Optional[str],
                 celular: Optional[str], numero_remision: str, estado: Optional[str] = None,
                 vr_dolar_dia: Optional[float] = None, modo_valores: Optional[str] = None) -> float:
    """Encabezado replicando el modelo REM_MODELO (posiciones absolutas)."""

    # --- Logo (modelo: caja x 447-571, top 14-123 => y 669-778) ---
    # La caja es la del modelo; el logo se centra dentro de ella sin deformarse.
    caja_ancho, caja_alto = 124, 109
    caja_x, caja_y = 447, ALTO - 123          # esquina inferior-izquierda de la caja
    if os.path.exists(RUTA_LOGO):
        try:
            c.drawImage(RUTA_LOGO, caja_x, caja_y,
                         width=caja_ancho, height=caja_alto,
                         preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception:
            pass  # el logo no debe impedir generar la remisión

    # --- Título (modelo: "Remision Salida" x328/top28; "de Material" x357/top44) ---
    # Con el flujo "Orden de Salida -> Remisión Aprobada", si la remisión está en
    # estado 'ORDEN_SALIDA' el documento es una ORDEN DE SALIDA (sin precios).
    if estado == "ORDEN_SALIDA":
        _texto(c, 322, 753, "ORDEN DE SALIDA", tam=11, negrita=True)
        _texto(c, 350, 738, "DE MATERIAL", tam=11, negrita=True)
    else:
        _texto(c, 328, 753, "Remision Salida", tam=11, negrita=True)
        _texto(c, 357, 738, "de Material", tam=11, negrita=True)

    # --- Datos del cliente (izquierda; etiquetas x33, líneas 122->307) ---
    _campo_con_linea(c, "Cliente:", cliente, MARGEN_IZQ, 758, 122, 307)
    _campo_con_linea(c, "Id Cliente:", documento, MARGEN_IZQ, 730, 122, 307)
    _campo_con_linea(c, "Celular:", celular, MARGEN_IZQ, 702, 122, 307)
    _campo_con_linea(c, "Dirección:", direccion, MARGEN_IZQ, 678, 122, 307)

    # --- N° de remisión (derecha, misma fila que Celular; línea 372->438) ---
    _texto(c, 359, 702, "N°", tam=10, negrita=True)
    c.setLineWidth(0.7)
    c.line(372, 694, 438, 694)
    if numero_remision:
        _texto(c, 375, 696, str(numero_remision), tam=9)

    # --- Fecha (derecha, bajo el N°; zona libre en el modelo) ---
    _texto(c, 359, 678, f"Fecha: {fecha}", tam=9, negrita=True)

    # --- Valor Dólar del Día: solo si el modo elegido incluye dólares ---
    # (modo 'DOLARES' o 'AMBAS'); en 'MONEDA_LOCAL' y 'SIN_VALORES' se omite.
    modo = (modo_valores or "").upper()
    if modo in {"DOLARES", "AMBAS"} and vr_dolar_dia:
        _texto(c, 359, 662, f"Valor Dólar del Día: ${float(vr_dolar_dia):,.2f}", tam=9, negrita=True)

    # La tabla inicia en una posición fija del modelo.
    return Y_TABLA


def _tabla_materiales(c: canvas.Canvas, y_inicio: float, items: List[Dict[str, Any]], *,
                      estado: Optional[str] = None, vr_dolar_dia: Optional[float] = None,
                      modo_valores: Optional[str] = None) -> float:
    """Tabla de materiales del documento.

    `modo_valores` (elección del usuario de Contabilidad) tiene prioridad y
    define las columnas financieras visibles de la Remisión Aprobada:
      - 'MONEDA_LOCAL'  -> MATERIAL, CANTIDAD KG, VR X KILO, VR TOTAL PESOS.
      - 'DOLARES'       -> MATERIAL, CANTIDAD KG, VR KILO DÓLAR, VR TOTAL DÓLAR.
      - 'AMBAS'         -> las seis columnas (locales + dólar).
      - 'SIN_VALORES'   -> solo MATERIAL y CANTIDAD KG.
    Cálculos aplicados cuando corresponde:
      VR X KILO = precio_unitario · VR TOTAL PESOS = precio_unitario * kg
      VR KILO DÓLAR = precio / vr_dolar_dia · VR TOTAL DÓLAR = (precio / tasa) * kg
    La fila TOTALES acumula las columnas visibles.

    Sin `modo_valores` (compatibilidad): estado='ORDEN_SALIDA' -> solo MATERIAL
    y CANTIDAD KG; cualquier otro estado -> columnas completas del modelo.
    """
    x = X_TABLA
    y = y_inicio
    modo = (modo_valores or "").upper()
    if modo == "SIN_VALORES":
        columnas, ancho_tabla = COLUMNAS_TABLA_ORDEN_SALIDA, ANCHO_TABLA_ORDEN_SALIDA
    elif modo == "MONEDA_LOCAL":
        columnas, ancho_tabla = COLUMNAS_TABLA_MONEDA_LOCAL, ANCHO_TABLA_MODOS["MONEDA_LOCAL"]
    elif modo == "DOLARES":
        columnas, ancho_tabla = COLUMNAS_TABLA_DOLARES, ANCHO_TABLA_MODOS["DOLARES"]
    elif modo == "AMBAS":
        columnas, ancho_tabla = COLUMNAS_TABLA_AMBAS, ANCHO_TABLA_MODOS["AMBAS"]
    else:
        columnas = COLUMNAS_TABLA_ORDEN_SALIDA if estado == "ORDEN_SALIDA" else COLUMNAS_TABLA
        ancho_tabla = ANCHO_TABLA_ORDEN_SALIDA if estado == "ORDEN_SALIDA" else ANCHO_TABLA
    dolar = float(vr_dolar_dia) if vr_dolar_dia else 0.0

    # Encabezado de tabla
    c.setLineWidth(0.7)
    c.rect(x, y - ALTO_FILA, ancho_tabla, ALTO_FILA)
    cx = x
    for nombre_col, ancho_col in columnas:
        c.rect(cx, y - ALTO_FILA, ancho_col, ALTO_FILA)
        _texto(c, cx, y - ALTO_FILA + 5, nombre_col, tam=7, negrita=True, centrado=True, ancho_centro=ancho_col)
        cx += ancho_col
    y -= ALTO_FILA

    total_kg = 0.0
    total_pesos = 0.0
    total_dolar = 0.0

    for i in range(FILAS_TABLA):
        cx = x
        item = items[i] if i < len(items) else None
        for nombre_col, ancho_col in columnas:
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
                elif nombre_col == "VR KILO DÓLAR":
                    precio = float(item.get("precio_unitario", 0) or 0)
                    if precio and dolar:
                        valor = f"{precio / dolar:,.2f}"
                elif nombre_col == "VR TOTAL DÓLAR":
                    cantidad = float(item.get("cantidad_kg", 0) or 0)
                    precio = float(item.get("precio_unitario", 0) or 0)
                    subtotal_dolar = (cantidad * precio) / dolar if dolar and precio else 0.0
                    if subtotal_dolar:
                        total_dolar += subtotal_dolar
                        valor = f"{subtotal_dolar:,.2f}"
                if valor:
                    _texto(c, cx + 3, y - ALTO_FILA + 5, valor, tam=8)
            cx += ancho_col
        y -= ALTO_FILA

    # Fila de totales
    cx = x
    for idx, (nombre_col, ancho_col) in enumerate(columnas):
        c.rect(cx, y - ALTO_FILA, ancho_col, ALTO_FILA)
        if idx == 0:
            _texto(c, cx, y - ALTO_FILA + 5, "TOTALES", tam=8, negrita=True, centrado=True, ancho_centro=ancho_col)
        elif nombre_col == "CANTIDAD KG":
            _texto(c, cx + 3, y - ALTO_FILA + 5, f"{total_kg:,.2f}", tam=8, negrita=True)
        elif nombre_col == "VR TOTAL PESOS":
            _texto(c, cx + 3, y - ALTO_FILA + 5, f"{total_pesos:,.0f}", tam=8, negrita=True)
        elif nombre_col == "VR TOTAL DÓLAR":
            _texto(c, cx + 3, y - ALTO_FILA + 5, f"{total_dolar:,.2f}", tam=8, negrita=True)
        cx += ancho_col
    y -= ALTO_FILA

    return y


def _observaciones(c: canvas.Canvas, y: float) -> float:
    """Observaciones con las 3 líneas del modelo (posiciones absolutas)."""
    _texto(c, 33, 427, "OBSERVACIONES:", tam=9.5, negrita=True)
    c.setLineWidth(0.5)
    c.line(177, 425, 574, 425)   # 1a línea (indentada, como el modelo)
    c.line(32, 410, 574, 410)    # 2a línea (ancho completo)
    c.line(32, 395, 574, 395)    # 3a línea (ancho completo)
    return 395


def _datos_conductor(c: canvas.Canvas, y: float, *, conductor: Optional[str], id_conductor: Optional[str],
                       celular_conductor: Optional[str], placa: Optional[str]) -> float:
    """Datos del conductor (modelo REM_MODELO).

    En el modelo las etiquetas están ALINEADAS A LA DERECHA y terminan todas en
    x=120, justo antes de la línea (que empieza en x=122); así nunca se solapan
    con la línea ni con el valor escrito sobre ella.
    """
    def etiqueta(texto: str, y_base: float) -> None:
        ancho = stringWidth(texto, "Helvetica-Bold", 9.5)
        _texto(c, 120 - ancho, y_base, texto, tam=9.5, negrita=True)

    def campo(texto_label: str, y_label: float, valor: Optional[str], y_linea: float) -> None:
        etiqueta(texto_label, y_label)
        c.setLineWidth(0.7)
        c.line(122, y_linea, 307, y_linea)
        if valor:
            _texto(c, 125, y_linea + 2, str(valor), tam=9.5)

    campo("CONDUCTOR:", 345, conductor, 334)
    campo("ID CONDUCTOR:", 315, id_conductor, 305)
    campo("CELULAR:", 286, celular_conductor, 275)

    # PATENTE O PLACA / VEHICULO (etiqueta en dos líneas, ambas alineadas a la derecha)
    etiqueta("PATENTE O PLACA", 263)
    etiqueta("VEHICULO", 249)
    c.setLineWidth(0.7)
    c.line(122, 245, 307, 245)
    if placa:
        _texto(c, 125, 247, str(placa), tam=9.5)
    return 245


def _firmas(c: canvas.Canvas) -> None:
    """Firmas con las posiciones del modelo (líneas a y=138, textos 128/112)."""
    c.setLineWidth(0.7)
    # Despachador (izquierda)
    c.line(32, 138, 242, 138)
    _texto(c, 33, 128, "NOMBRE Y FIRMA DESPACHADOR FERROMA", tam=8.5)
    _texto(c, 33, 112, "ID:", tam=8.5)
    # Conductor (derecha)
    c.line(372, 138, 574, 138)
    _texto(c, 374, 128, "NOMBRE Y FIRMA CONDUCTOR", tam=8.5)
    _texto(c, 374, 112, "ID:", tam=8.5)


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
    estado: Optional[str] = None,
    vr_dolar_dia: Optional[float] = None,
    modo_valores: Optional[str] = None,
) -> str:
    """
    Genera la remisión de salida de material de FERROMA en formato PDF,
    replicando el modelo REM_MODELO.pdf, y la guarda en `ruta_salida`.

    Flujo "Orden de Salida -> Remisión Aprobada":
      - estado='ORDEN_SALIDA': el documento es una ORDEN DE SALIDA DE MATERIAL
        (título cambiado) y se OMITEN todas las columnas/campos financieros
        (precio por kilo, totales en pesos y conversiones a dólar). Solo se
        muestran Material, Cantidad (kg), datos del Cliente, Conductor y Vehículo.
      - estado='APROBADA' (o None, comportamiento legacy): PDF del modelo con
        precios por kilo, totales en pesos y conversiones a dólar (si se pasa
        `vr_dolar_dia` y los items traen `precio_unitario`).

    `modo_valores` (elección del usuario de Contabilidad) controla qué valores
    se imprimen en la Remisión Aprobada:
      - 'MONEDA_LOCAL': Material, Cantidad (kg), VR X KILO, VR TOTAL PESOS.
      - 'DOLARES':      Material, Cantidad (kg), VR KILO DÓLAR, VR TOTAL DÓLAR
                        (encabezado con 'Valor Dólar del Día: $XXXX').
      - 'AMBAS':        las seis columnas (locales + dólar) y el valor del dólar.
      - 'SIN_VALORES':  solo Material y Cantidad (kg), sin campos financieros.
    Tiene prioridad sobre la selección de columnas por `estado`; si no se pasa,
    se conserva el comportamiento por estado descrito arriba.
    """
    items = items or []

    c = canvas.Canvas(ruta_salida, pagesize=letter)
    prefijo_doc = "OrdenSalida" if estado == "ORDEN_SALIDA" else "Remision"
    c.setTitle(f"{prefijo_doc} {numero_remision}")

    y = _encabezado(
        c, fecha=fecha, cliente=cliente, documento=documento,
        direccion=direccion, celular=celular, numero_remision=numero_remision,
        estado=estado, vr_dolar_dia=vr_dolar_dia, modo_valores=modo_valores,
    )
    y = _tabla_materiales(c, y, items, estado=estado, vr_dolar_dia=vr_dolar_dia,
                          modo_valores=modo_valores)
    y = _observaciones(c, y)
    _datos_conductor(
        c, y, conductor=conductor, id_conductor=id_conductor,
        celular_conductor=celular_conductor, placa=placa,
    )
    _firmas(c)

    c.showPage()
    c.save()
    return ruta_salida