"""Script: repara router.py - elimina elif mal ubicado e inyecta en lugar correcto."""
import pathlib

ruta = pathlib.Path("handlers/router.py")
texto = ruta.read_text(encoding="utf-8")

# ── 1. Eliminar el elif mal ubicado (líneas 527-534 aprox) ─────────────────
old_bad = """
        elif accion["tipo"] == pdf_handler.TIPO_SELECCION_PDF:
            respuesta_texto = await pdf_handler.manejar_respuesta_seleccion_pdf(
                telefono=telefono, respuesta=texto,
        )
        if respuesta_texto:
            await enviar_mensaje_whatsapp(telefono, respuesta_texto)
        return

    elif texto in {"3", "Anular Inventario"}"""

new_bad = """
    elif texto in {"3", "Anular Inventario"}"""

if old_bad in texto:
    texto = texto.replace(old_bad, new_bad, 1)
    print("1/2 OK: elif mal ubicado eliminado")
else:
    print("1/2 SKIP: texto no encontrado")
    # Buscar variante
    if "pdf_handler.TIPO_SELECCION_PDF" in texto:
        print("   -> pdf_handler.TIPO_SELECCION_PDF SÍ está en router.py")
        import re
        m = re.search(r'        elif accion\["tipo"\] == pdf_handler\.TIPO_SELECCION_PDF:.*?return\n\n    elif', texto, re.DOTALL)
        if m:
            print(f"   -> Encontrado en posición {m.start()}-{m.end()}")
            # Mostrar contexto
            print("   ->", repr(texto[m.start()-50:m.end()+50]))

# ── 2. Inyectar elif en lugar correcto ─────────────────────────────────────
# Después del último elif del bloque accion (crear_material_paso, línea ~402)
old_after = '''            elif accion["tipo"] == "crear_material_paso":
                respuesta_texto = await materiales_handler.procesar_flujo_material(
                    telefono=telefono, usuario_id=usuario_id, bodega_id=bodega_id,
                    contexto=contexto, accion=accion, texto=texto,
                    texto_normalizado=texto_normalizado)
                if respuesta_texto is MANEJADO:
                    return
        except Exception'''

new_after = '''            elif accion["tipo"] == "crear_material_paso":
                respuesta_texto = await materiales_handler.procesar_flujo_material(
                    telefono=telefono, usuario_id=usuario_id, bodega_id=bodega_id,
                    contexto=contexto, accion=accion, texto=texto,
                    texto_normalizado=texto_normalizado)
                if respuesta_texto is MANEJADO:
                    return
            elif accion["tipo"] == pdf_handler.TIPO_SELECCION_PDF:
                respuesta_texto = await pdf_handler.manejar_respuesta_seleccion_pdf(
                    telefono=telefono, respuesta=texto,
                )
                if respuesta_texto:
                    await enviar_mensaje_whatsapp(telefono, respuesta_texto)
                return
        except Exception'''

if "pdf_handler.TIPO_SELECCION_PDF" not in texto and old_after in texto:
    texto = texto.replace(old_after, new_after, 1)
    print("2/2 OK: elif pdf inyectado en lugar correcto")
elif "pdf_handler.TIPO_SELECCION_PDF" in texto:
    print("2/2 SKIP: ya injectado")
else:
    print("2/2 SKIP: old_after no encontrado")

ruta.write_text(texto, encoding="utf-8")
print("Router.py GUARDADO")
