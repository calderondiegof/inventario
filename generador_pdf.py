"""Módulo para generar PDFs de remisión de material"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors


def generar_remision_pdf(
    fecha: str,
    cliente: str,
    documento: Optional[str] = None,
    placa: Optional[str] = None,
    conductor: Optional[str] = None,
    id_conductor: Optional[str] = None,          # <--- Añadido
    celular_conductor: Optional[str] = None,     # <--- Añadido
    celular: Optional[str] = None,
    items: List[Dict[str, Any]] = None,
    numero_remision: str = "001",
    empresa_nombre: str = "FERROMA S.A.S",
    bodega_id: int = 1,
) -> BytesIO:
    """
    Genera un PDF de remisión/factura de salida de material.
    """
    if items is None:
        items = []
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch,
        leftMargin=0.5*inch,
        rightMargin=0.5*inch,
    )
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=14,
        textColor=colors.black,
        spaceAfter=6,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
    )
    
    header_style = ParagraphStyle(
        'Header',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.black,
        spaceAfter=2,
    )
    
    story = []
    
    # Encabezado
    story.append(Paragraph(empresa_nombre, title_style))
    story.append(Paragraph("REMISIÓN SALIDA DE MATERIAL", header_style))
    story.append(Spacer(1, 0.15*inch))
    
    fecha_obj = datetime.strptime(fecha, "%Y-%m-%d")
    fecha_formato = fecha_obj.strftime("%d de %B de %Y").upper()
    
    # Información general incluyendo conductor, ID y celular del conductor
    info_data = [
        ["FECHA:", fecha_formato, "RM:" + numero_remision],
        ["CLIENTE:", cliente, "PLACA:", placa or ""],
        ["CONDUCTOR:", conductor or "", "ID COND:", id_conductor or ""],
        ["CEL. COND:", celular_conductor or "", "CEL. CLIENTE:", celular or ""],
        ["DOCUMENTO:", documento or "", "BODEGA:", str(bodega_id)],
    ]
    
    info_table = Table(info_data, colWidths=[1.2*inch, 2.5*inch, 1*inch, 1.8*inch])
    info_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    
    story.append(info_table)
    story.append(Spacer(1, 0.2*inch))
    
    # Tabla de materiales
    material_data = [
        ["MATERIAL", "CANTIDAD\nEN KG.", "CANT.\nBOLSONES", "CANT.\nLONAS", "VR. MATERIAL\nX KILO", "VR. TOTAL", "DOLARES"]
    ]
    
    total_kg = 0
    total_valor = 0
    
    for item in items:
        material = item.get("material_nombre", "").upper()
        cantidad = float(item.get("cantidad_kg", 0))
        precio = float(item.get("precio_unitario", 0))
        valor_total = cantidad * precio
        
        total_kg += cantidad
        total_valor += valor_total
        
        material_data.append([
            material,
            f"{cantidad:,.0f}",
            "",
            "",
            f"${precio:,.0f}" if precio > 0 else "-",
            f"${valor_total:,.0f}" if valor_total > 0 else "-",
            "",
        ])
    
    material_data.append([
        "TOTALES",
        f"{total_kg:,.0f}",
        "0",
        "0",
        "",
        f"${total_valor:,.0f}",
        "",
    ])
    
    material_table = Table(material_data, colWidths=[1.5*inch, 0.9*inch, 0.8*inch, 0.8*inch, 1.2*inch, 1.2*inch, 0.8*inch])
    material_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 7),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    
    story.append(material_table)
    story.append(Spacer(1, 0.3*inch))
    
    # Observaciones y firmas
    obs_data = [
        ["OBS:", ""],
        ["", ""],
        ["FERROMA S.A.S", "ENTREGADO POR:"],
        ["DESPACHADO POR:", ""],
    ]
    
    obs_table = Table(obs_data, colWidths=[1.5*inch, 4.5*inch])
    obs_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    
    story.append(obs_table)
    
    doc.build(story)
    buffer.seek(0)
    return buffer


def generar_remision_pdf_archivo(
    filepath: str,
    fecha: str,
    cliente: str,
    documento: Optional[str] = None,
    placa: Optional[str] = None,
    conductor: Optional[str] = None,
    id_conductor: Optional[str] = None,          # <--- Añadido
    celular_conductor: Optional[str] = None,     # <--- Añadido
    celular: Optional[str] = None,
    items: List[Dict[str, Any]] = None,
    numero_remision: str = "001",
    empresa_nombre: str = "FERROMA S.A.S",
    bodega_id: int = 1,
) -> str:
    """Genera PDF y lo guarda en un archivo"""
    pdf_buffer = generar_remision_pdf(
        fecha=fecha,
        cliente=cliente,
        documento=documento,
        placa=placa,
        conductor=conductor,
        id_conductor=id_conductor,
        celular_conductor=celular_conductor,
        celular=celular,
        items=items,
        numero_remision=numero_remision,
        empresa_nombre=empresa_nombre,
        bodega_id=bodega_id,
    )
    
    with open(filepath, 'wb') as f:
        f.write(pdf_buffer.getvalue())
    
    return filepath