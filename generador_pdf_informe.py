"""Generador de PDF del informe por material (gráfico + tabla).

Patrón: igual que remisiones (reportlab + logo Ferroma). El gráfico se dibuja
con matplotlib y se incrusta en el PDF. Recibe el dict `informe` que produce
`inventario.obtener_informe_material` (el mismo del texto ya verificado).

Colores: entradas AZUL, salidas AMARILLO, saldo VERDE (positivo) / ROJO (negativo).
"""
from __future__ import annotations

import io
import os
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

RUTA_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "logo_ferroma.jpeg")

AZUL = "#1f5fbf"
AMARILLO = "#f2c40f"
VERDE = "#2ca02c"
ROJO = "#d9534f"


def _grafico_informe(informe: Dict[str, Any]) -> io.BytesIO:
    """Barras: azules para entradas, amarillas para salidas y una final de
    saldo (verde si >= 0, rojo si < 0). Devuelve el PNG en memoria."""
    conceptos: List[str] = []
    valores: List[float] = []
    colores: List[str] = []

    for it in informe.get("entradas", []):
        conceptos.append(it["etiqueta"])
        valores.append(float(it["kg"]))
        colores.append(AZUL)
    for it in informe.get("salidas", []):
        conceptos.append(it["etiqueta"])
        valores.append(-float(it["kg"]))  # salidas hacia abajo/negativas
        colores.append(AMARILLO)
    saldo = float(informe.get("movimiento", 0.0))
    conceptos.append("Saldo")
    valores.append(saldo)
    colores.append(VERDE if saldo >= 0 else ROJO)

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    barras = ax.bar(range(len(conceptos)), valores, color=colores)
    ax.set_xticks(range(len(conceptos)))
    ax.set_xticklabels(conceptos, rotation=20, ha="right", fontsize=8)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("kg")
    ax.set_title(f"Entradas vs Salidas — {informe['material']} "
                 f"({informe['fecha_desde']} al {informe['fecha_hasta']})",
                 fontsize=10)
    for barra, v in zip(barras, valores):
        ax.annotate(f"{v:,.2f}", (barra.get_x() + barra.get_width() / 2, v),
                    ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=7.5)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


def generar_informe_material_pdf(ruta_salida: str, informe: Dict[str, Any]) -> str:
    """Genera el PDF del informe: encabezado con logo, gráfico de barras y
    tabla de datos debajo. Devuelve `ruta_salida`."""
    c = rl_canvas.Canvas(ruta_salida, pagesize=letter)
    ancho, alto = letter

    # --- Logo (esquina derecha superior) ---
    if os.path.exists(RUTA_LOGO):
        try:
            c.drawImage(RUTA_LOGO, ancho - 150, alto - 110,
                        width=118, height=80,
                        preserveAspectRatio=True, anchor="c", mask="auto")
        except Exception:
            pass  # el logo no debe impedir generar el PDF

    # --- Título y rango de fechas ---
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, alto - 60, f"Reporte Material {informe['material']}")
    c.setFont("Helvetica", 10)
    c.drawString(50, alto - 78,
                 f"Período: {informe['fecha_desde']} al {informe['fecha_hasta']}"
                 f"   •   Bodega #{informe['bodega_id']}")

    # --- Gráfico ---
    buf = _grafico_informe(informe)
    img_ancho, img_alto = 512, 288
    y_img = alto - 110 - img_alto
    c.drawImage(ImageReader(buf), 42, y_img, width=img_ancho, height=img_alto,
                preserveAspectRatio=True, anchor="c", mask="auto")

    # --- Tabla de datos debajo del gráfico ---
    y = y_img - 24

    def fila(etiqueta: str, valor: str, negrita: bool = False) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold" if negrita else "Helvetica", 9.5)
        c.setLineWidth(0.4)
        c.line(50, y - 3, ancho - 50, y - 3)
        c.drawString(55, y - 14, etiqueta)
        c.drawRightString(ancho - 55, y - 14, valor)
        y -= 20

    fila("Saldo inicial", f"{informe['saldo_inicial']:,.2f} kg", negrita=True)
    for it in informe.get("entradas", []):
        fila(f"  Entrada — {it['etiqueta']}", f"{float(it['kg']):,.2f} kg")
    fila("Total entradas", f"{informe['total_entradas']:,.2f} kg", negrita=True)
    for it in informe.get("salidas", []):
        fila(f"  Salida — {it['etiqueta']}", f"-{float(it['kg']):,.2f} kg")
    fila("Total salidas", f"-{informe['total_salidas']:,.2f} kg", negrita=True)
    fila("Movimiento del período", f"{informe['movimiento']:+,.2f} kg")
    saldo_color = VERDE if informe["saldo_final"] >= 0 else ROJO
    c.setFillColorRGB(int(saldo_color[1:3], 16) / 255,
                       int(saldo_color[3:5], 16) / 255,
                       int(saldo_color[5:7], 16) / 255)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(55, y - 16,
                 f"Saldo final ({informe['fecha_hasta']}):")
    c.drawRightString(ancho - 55, y - 16,
                      f"{informe['saldo_final']:,.2f} kg")
    c.setFillColorRGB(0, 0, 0)

    c.showPage()
    c.save()
    return ruta_salida
