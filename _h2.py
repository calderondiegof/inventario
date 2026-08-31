"""Generador pdf_handler.py (parte 2/2): funciones async"""
import pathlib
A = r'''
async def manejar_comando_pdf(
    texto: str, telefono: str, usuario_id: int = 0, rol_usuario: str = "usuario_administrador"
) -> str:
    """Procesa solicitudes de envio/reimpresion de PDF de remision.

    Formatos:
      pdf REM_117 1       -> fast-track: numero + modo -> genera y envia directo
      pdf REM_117 cop     -> fast-track con alias
      pdf REM_117         -> wizard: pregunta modo, luego genera y envia
      pdf                 -> lista remisiones existentes
    """
    partes = texto.strip().split()
    if len(partes) == 1:
        return await _listar_para_seleccion(telefono, usuario_id)
    numero = partes[1]
    if len(partes) >= 3:
        return await _fast_track(numero, partes[2], telefono, rol_usuario)
    return await _iniciar_wizard_modo(numero, telefono, usuario_id)


async def _fast_track(numero: str, modo_raw: str, telefono: str,
                      rol_usuario: str) -> str:
    if modo_raw.lower() not in MAPA_MODOS_IMPRESION and modo_raw not in MAPA_MODOS_IMPRESION:
        return (f"El modo '{modo_raw}' no es valido. "
                "Usa 1 (COP), 2 (USD), 3 (ambas) o 4 (sin valores).")
    try:
        ruta, modo, advertencia = await _get_service().reimprimir_pdf_dinamico(
            numero_remision=numero, modo_solicitado=modo_raw, rol_usuario=rol_usuario)
    except PdfRemisionError as exc:
        return f"No se encontro la remision '{numero}'.\n\n{exc}"
    msgs = []
    if advertencia: msgs.append(advertencia)
    msgs.append(f"Generando PDF en modo {modo.value}...")
    if msgs: await enviar_mensaje_whatsapp(telefono, "\n".join(msgs))
    await _enviar_documento_y_limpiar(ruta, telefono, numero)
    return ""


async def _iniciar_wizard_modo(numero: str, telefono: str, usuario_id: int) -> str:
    await guardar_contexto(
        usuario_id,
        {"accion_pendiente": {"tipo": TIPO_MODO_IMPRESION},
         "numero_remision": numero},
    )
    await enviar_botones_whatsapp(
        destino=telefono,
        texto=_mensaje_pregunta_modo(numero),
        titulo_boton="Como imprimir?",
        filas=[
            (ModoImpresion.MONEDA_LOCAL.value, MODO_LABELS[ModoImpresion.MONEDA_LOCAL]),
            (ModoImpresion.DOLARES.value,      MODO_LABELS[ModoImpresion.DOLARES]),
            (ModoImpresion.AMBAS.value,         MODO_LABELS[ModoImpresion.AMBAS]),
            (ModoImpresion.SIN_VALORES.value,   MODO_LABELS[ModoImpresion.SIN_VALORES]),
        ],
    )
    return ""


async def manejar_respuesta_modo_impresion(
    telefono: str, respuesta: str, contexto: dict,
    rol_usuario: str = "usuario_administrador",
) -> str:
    numero = (contexto or {}).get("numero_remision")
    if not numero:
        return "No encontre el numero de remision. Usa 'pdf' para comenzar de nuevo."
    clave = respuesta.strip().lower()
    if clave not in MAPA_MODOS_IMPRESION:
        await enviar_botones_whatsapp(
            destino=telefono,
            texto=f"Modo '{respuesta}' no reconocido.\n\n{_mensaje_pregunta_modo(numero)}",
            titulo_boton="Como imprimir?",
            filas=[
                (ModoImpresion.MONEDA_LOCAL.value, MODO_LABELS[ModoImpresion.MONEDA_LOCAL]),
                (ModoImpresion.DOLARES.value,      MODO_LABELS[ModoImpresion.DOLARES]),
                (ModoImpresion.AMBAS.value,         MODO_LABELS[ModoImpresion.AMBAS]),
                (ModoImpresion.SIN_VALORES.value,   MODO_LABELS[ModoImpresion.SIN_VALORES]),
            ],
        )
        return ""
    try:
        ruta, modo, advertencia = await _get_service().reimprimir_pdf_dinamico(
            numero_remision=numero, modo_solicitado=clave, rol_usuario=rol_usuario)
    except PdfRemisionError as exc:
        return f"No se encontro la remision '{numero}'.\n\n{exc}"
    await _enviar_documento_y_limpiar(ruta, telefono, numero, advertencia)
    return ""


async def _enviar_documento_y_limpiar(
    ruta: Path, telefono: str, numero: str,
    advertencia: Optional[str] = None,
) -> None:
    if advertencia:
        await enviar_mensaje_whatsapp(telefono, advertencia)
    await enviar_documento_whatsapp(
        destino=telefono, ruta_archivo=str(ruta), nombre_documento=f"{numero}.pdf")


async def _listar_para_seleccion(telefono: str, usuario_id: int = 0) -> str:
    remisiones = _get_service().listar_remisiones_con_pdf(limite=10)
    if not remisiones:
        return ("No hay remisiones con PDF disponibles. "
                "Usa 'pdf <numero>' para generar una nueva.")
    filas = construir_filas_listado_pdf(remisiones)
    if len(filas) <= 10:
        await enviar_lista_whatsapp(
            destino=telefono,
            texto="Selecciona una remision para reimprimir su PDF:",
            titulo_boton="Ver PDF", filas=filas,
            titulo_lista="Remisiones con PDF",
        )
    else:
        lineas = ["Remisiones con PDF:"]
        for r in remisiones[:20]:
            fecha = r.fecha_creacion[:10] if r.fecha_creacion else "-"
            lineas.append(
                f"  - {r.numero_remision}  |  "
                f"{(r.cliente or 'Sin cliente'):<30}  |  {fecha}")
        lineas.append("")
        lineas.append("Usa 'pdf <numero> <modo>' para reimprimir. Ej: pdf REM_117 1")
        await enviar_mensaje_whatsapp(telefono, "\n".join(lineas))
    if usuario_id:
        await guardar_contexto(usuario_id, {"accion_pendiente": {"tipo": TIPO_SELECCION_PDF}})
    return ""


async def manejar_respuesta_seleccion_pdf(telefono: str, respuesta: str) -> str:
    respuesta = (respuesta or "").strip()
    if not respuesta:
        return "Seleccion vacia. Usa 'pdf' para ver el listado de nuevo."
    return await _iniciar_wizard_modo(respuesta, telefono, usuario_id=0)
'''
p = pathlib.Path("handlers/pdf_handler.py")
p.write_text(p.read_text(encoding="utf-8") + A, encoding="utf-8")
print("h2 OK:", p.stat().st_size, "bytes")
