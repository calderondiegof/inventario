"""Paso 4: _generar_pdf_desde_datos + construir_filas_listado_pdf"""
import pathlib
p = pathlib.Path("services/pdf_remision_service.py")
p.write_text(p.read_text(encoding="utf-8") + """


def _generar_pdf_desde_datos(ruta_salida: str, datos: Dict, cliente: Dict,
                             conductor: Dict, vr_dolar_dia: float, modo_valores: str) -> None:
    from generador_pdf import generar_remision_pdf_archivo
    generar_remision_pdf_archivo(
        ruta_salida,
        fecha=str(datos.get("fecha_operacion") or ""),
        cliente=cliente.get("nombre", ""),
        documento=cliente.get("identificacion"),
        direccion=cliente.get("direccion"),
        celular=cliente.get("telefono"),
        placa=conductor.get("placa"),
        conductor=conductor.get("nombre"),
        id_conductor=conductor.get("identificacion"),
        celular_conductor=conductor.get("telefono"),
        items=datos.get("items") or [],
        numero_remision=datos.get("numero_remision", "SIN-NUMERO"),
        bodega_id=datos.get("bodega_id"),
        estado=datos.get("estado"),
        vr_dolar_dia=vr_dolar_dia,
        modo_valores=modo_valores,
    )


def construir_filas_listado_pdf(remisiones: List[RemisionPdf]) -> List[Tuple[str, str, str]]:
    filas: List[Tuple[str, str, str]] = []
    for r in remisiones[:10]:
        num = r.numero_remision
        cliente = (r.cliente or "Sin cliente").strip()
        fecha = ""
        if r.fecha_creacion:
            try:
                dt = datetime.fromisoformat(str(r.fecha_creacion).replace("Z", ""))
                fecha = dt.strftime("%Y-%m-%d")
            except ValueError:
                fecha = str(r.fecha_creacion)[:10]
        filas.append((num, num[:24], f"{cliente[:40]}{' - ' + fecha if fecha else ' '}"))
    return filas
""", encoding="utf-8")
print("g4 OK:", p.stat().st_size, "bytes")
