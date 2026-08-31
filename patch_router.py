"""Script: inyecta el comando 'pdf' y la rama de accion_pendiente en router.py."""
import pathlib

ruta = pathlib.Path("handlers/router.py")
texto = ruta.read_text(encoding="utf-8")
original = texto

# ── 1. Importar pdf_handler ─────────────────────────────────────────────────
OLD_IMP = "from handlers import remisiones_handler"
NEW_IMP = "from handlers import remisiones_handler\nfrom handlers import pdf_handler"
if OLD_IMP in texto and "from handlers import pdf_handler" not in texto:
    texto = texto.replace(OLD_IMP, NEW_IMP, 1)
    print("1/3 OK: import pdf_handler añadido")
else:
    print("1/3 SKIP: import ya presente o patrón no encontrado")

# ── 2. Rama accion_pendiente para seleccion_pdf ──────────────────────────────
# Buscar la línea que contiene 'elif accion["tipo"] in remisiones_handler.TIPOS_APROBACION'
# e inyectar justo después el manejo de pdf_handler.TIPO_SELECCION_PDF.
RAMA_APROB = "elif accion[\"tipo\"] in remisiones_handler.TIPOS_APROBACION:"
INYECCION_PENDIENTE = """elif accion["tipo"] in remisiones_handler.TIPOS_APROBACION:
"""
# Buscar si ya existe la rama de seleccion_pdf
if f'pdf_handler.TIPO_SELECCION_PDF' not in texto and RAMA_APROB in texto:
    # Encontrar la posición y buscar el bloque elif completo
    idx = texto.find(RAMA_APROB)
    # Buscar el siguiente 'elif accion' o 'else:' después de TIPOS_APROBACION
    resto = texto[idx + len(RAMA_APROB):]
    # Encontrar dónde termina este elif (buscar el siguiente elif/else/return al mismo nivel)
    # Simplificado: buscamos el siguiente elif accion o else: que esté al inicio de línea
    import re
    sig = re.search(r'\n    (elif |else:)', resto)
    if sig:
        pos_ins = idx + len(RAMA_APROB) + sig.start()
        bloque = """
        elif accion["tipo"] == pdf_handler.TIPO_SELECCION_PDF:
            respuesta_texto = await pdf_handler.manejar_respuesta_seleccion_pdf(
                telefono=telefono, respuesta=texto,
            )
            if respuesta_texto:
                await enviar_mensaje_whatsapp(telefono, respuesta_texto)
            return
"""
        texto = texto[:pos_ins] + bloque + texto[pos_ins:]
        print("2/3 OK: rama accion_pendiente pdf inyectada")
    else:
        print("2/3 SKIP: no se encontró dónde inyectar")
elif f'pdf_handler.TIPO_SELECCION_PDF' in texto:
    print("2/3 SKIP: rama ya presente")
else:
    print("2/3 SKIP: patrón no encontrado")

# ── 3. Trigger de comando "pdf" / "pdf <n>" en el flujo principal ───────────
# Buscar la zona donde está el bloque de 'remisiones_handler.procesar_wizard_registro'
# e inyectar ANTES de ese fallback un check para comandos pdf.
TRIGGER_WIZARD = "await remisiones_handler.procesar_wizard_registro("
if TRIGGER_WIZARD in texto:
    idx = texto.find(TRIGGER_WIZARD)
    # Buscar el return o línea anterior para colocar el if antes
    # Buscar hacia atrás hasta encontrar un bloque 'if texto_normalizado in'
    bloque_antes = texto[:idx]
    # Encontrar el último 'if texto_normalizado in' o 'elif texto' en las ~30 líneas anteriores
    import re
    m = re.search(r'\n    (if |elif )(texto[_\w]*|)[a-z_\.]+ in {', bloque_antes[max(0, idx-3000):idx])
    if m:
        pos_ins = idx
        bloque_cmd = """
    # ── Comando PDF ────────────────────────────────────────────────────────────
    if texto_normalizado.startswith("pdf"):
        resp = await pdf_handler.manejar_comando_pdf(
            texto=texto, telefono=telefono, usuario_id=usuario_id,
        )
        if resp:
            await enviar_mensaje_whatsapp(telefono, resp)
        return

"""
        texto = texto[:pos_ins] + bloque_cmd + texto[pos_ins:]
        print("3/3 OK: trigger pdf inyectado")
    else:
        print("3/3 SKIP: no se encontró zona de inyección")
else:
    print("3/3 SKIP: procesar_wizard_registro no encontrado")

if texto != original:
    ruta.write_text(texto, encoding="utf-8")
    print("Router.py GUARDADO")
else:
    print("Router.py sin cambios")
