import re
content = open("handlers/pdf_handler.py", encoding="utf-8").read()

# Corregir _listar_remisiones
old1 = """    await enviar_lista_whatsapp(
        destino=telefono,
        header_title="Remisiones", header_subtitle="Ultimas remisiones",
        body_text="Selecciona una remision para reimprimir:",
        boton_principal="Ver remisiones",
        opciones=[{"title": titulo, "description": desc} for _, titulo, desc in filas],
    )"""
new1 = """    await enviar_lista_whatsapp(
        destino=telefono,
        texto="Selecciona una remision para reimprimir:",
        titulo_boton="Ver remisiones",
        filas=[(titulo, titulo, desc) for _, titulo, desc in filas],
    )"""
if old1 in content:
    content = content.replace(old1, new1)
    print("FIX 1: _listar_remisiones OK")
else:
    print("WARN: old1 no encontrado en _listar_remisiones")

# Corregir manejar_comando_pdf
old2 = """    await enviar_lista_whatsapp(
        destino=telefono,
        header_title="Modo de impresion", header_subtitle=numero_raw,
        body_text=_pregunta_modo(numero_raw),
        boton_principal="Seleccionar modo", opciones=_OPCIONES_MODO,
    )"""
new2 = """    filas_modo = [(
        opt["id"], opt["title"], ""
    ) for opt in _OPCIONES_MODO]
    await enviar_lista_whatsapp(
        destino=telefono,
        texto=_pregunta_modo(numero_raw),
        titulo_boton="Seleccionar modo",
        filas=filas_modo,
        titulo_lista="Modo de impresion",
    )"""
if old2 in content:
    content = content.replace(old2, new2)
    print("FIX 2: manejar_comando_pdf OK")
else:
    print("WARN: old2 no encontrado en manejar_comando_pdf")

open("handlers/pdf_handler.py", "w", encoding="utf-8").write(content)
print("DONE")
