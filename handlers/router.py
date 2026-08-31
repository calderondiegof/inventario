"""Router central del bot: lee el mensaje, consulta el estado del usuario y delega al handler correspondiente."""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import inventario, supabase
from core.contexto import fecha_local_mensaje, guardar_contexto
from core.whatsapp import (
    enviar_botones_whatsapp, enviar_imagen_whatsapp, enviar_lista_whatsapp,
    enviar_mensaje_whatsapp,
)
from handlers import MANEJADO
from handlers import clientes_handler, conductores_handler, materiales_handler
from handlers import remisiones_handler
from handlers.consultas_handler import (
    enviar_grafico_inventario, enviar_inventario_total, enviar_reporte_diario,
    iniciar_inventario_total, iniciar_reporte_por_fecha,
    pedir_movimientos_material,
)
from handlers.materiales_handler import iniciar_creacion, iniciar_creacion_material
from handlers.remisiones_handler import (
    TRIGGERS_ORDENES_SALIDA, iniciar_aprobacion_orden_salida,
)
from reporte_grafico import generar_y_subir_grafico_stock
from services.inventario_service import (
    construir_lista_texto_whatsapp, resolver_entrada_material,
)
from utils.text_normalizer import normalizar as _normalizar_texto
from utils.parsers import parsear_fecha_colombiana
from utils.whatsapp_formatter import formatear_movimientos_material

logger = logging.getLogger(__name__)


SUB_MENU_INVENTARIO = [
    ("inv_total", "Inventario total"),
    ("inv_movs", "Ver movimientos"),
    ("inv_hoy", "Reporte de hoy"),
]


# Saludos simples que el bot entiende en cualquier momento fuera de un wizard
# activo. La comparación se hace sobre `texto_normalizado` (sin tildes, en
# minúsculas, sin espacios extra) para que "Hola", "  hola  " y "HOLA" se
# traten igual. NO se incluye "menu" como saludo porque "menu" ya es un
# trigger del wizard 'crear_menu' (router 269); la captura de "menu" en el
# atajo de saludos lo atendería antes de que esa rama se evalúe y bloquearía
# el submenú unificado de creación.
SALUDOS_BIENVENIDA = frozenset({
    "hola",
    "holaa",
    "holaaa",
    "hi",
    "hello",
    "buenas",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
    "buen dia",
    "inicio",
    "empezar",
    "empezamos",
    "arrancamos",
    "que tal",
    "que mas",
    "saludos",
    "saludame",
    "salu2",
    "ola",       # "ola" típico cuando el teclado móvil no tiene ñ
    "klk",       # "klk" muy usado en Colombia
    "sirva",
})


_MENSAJE_BIENVENIDA = (
    "¡Hola! 👋 Soy el bot de inventario. Puedo ayudarte con:\n\n"
    "1️⃣ Ingresar inventario (entrada, selección/arreglo, salida/venta)\n"
    "2️⃣ Ver inventario (totales, movimientos, reporte de hoy)\n"
    "3️⃣ Anular o corregir una remisión\n\n"
    "También puedes escribirme lo que quieras en lenguaje natural, por "
    "ejemplo:\n"
    "• \"Hoy entró Revuelto de Cooperativa 1500 kg\"\n"
    "• \"Vendí Cobre 100 kg a Acero SA\"\n"
    "• \"Seleccioné Carter 40, Cable 20\"\n\n"
    "Cuando quieras empezar, responde con el número o la opción. "
    "Para volver a este menú en cualquier momento, escribe *hola*."
)


async def procesar_un_mensaje(message: Dict[str, Any], contactos: List[Dict[str, Any]]) -> None:
    logger.info(f"🔄 Iniciando procesamiento de mensaje: {message}")
    tipo_mensaje = message.get("type")
    # 1) Extracción del texto entrante con cadena de fallbacks INDEPENDIENTE
    #    del campo 'type' (texto, botón interactivo o lista interactiva), y
    #    normalización única al inicio: así ninguna rama posterior de la
    #    función puede usar `texto`/`texto_normalizado` sin definir
    #    (evita el UnboundLocalError en los triggers de comandos directos).
    texto_recibido = (
        message.get("text", {}).get("body", "")
        or message.get("interactive", {}).get("button_reply", {}).get("id", "")
        or message.get("interactive", {}).get("list_reply", {}).get("id", "")
    )
    texto_normalizado = _normalizar_texto(texto_recibido)
    texto = texto_recibido.strip()
    logger.info(f"Texto recibido procesado: '{texto_normalizado}'")
    if tipo_mensaje == "interactive":
        interactivo = message.get("interactive", {})
        if interactivo.get("type") not in {"button_reply", "list_reply"}:
            logger.warning(f"⚠️ Tipo interactivo no soportado: {interactivo.get('type')}")
    if not texto:
        logger.warning(f"⚠️ Mensaje sin texto soportado: tipo={tipo_mensaje}")
        return
    telefono = str(message.get("from", "")).replace("+", "")
    logger.info(f"📱 Teléfono: {telefono}, Texto: {texto}")
    if not telefono or not texto:
        logger.warning(f"❌ Teléfono o texto vacío - Abortando")
        return
    usuarios = await asyncio.to_thread(lambda: supabase.table("usuarios").select("*,bodegas(nombre)").eq("telefono_whatsapp", telefono).execute())
    if not usuarios.data:
        await enviar_mensaje_whatsapp(telefono, "Acceso denegado: número no registrado.")
        return
    usuario = usuarios.data[0]
    usuario_id, bodega_id = usuario["id"], usuario.get("bodega_asignada_id")
    if not bodega_id:
        await enviar_mensaje_whatsapp(telefono, "Tu usuario no tiene una bodega asignada.")
        return
    contexto = usuario.get("contexto_operacion") or {}
    if texto.lower() in {"cancelar", "limpiar", "reiniciar"}:
        contexto["borrador_pendiente"] = {}
        contexto["accion_pendiente"] = {}
        contexto["campo_esperado"] = None
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "Operación cancelada.")
        return

    accion = contexto.get("accion_pendiente") or {}
    if accion.get("tipo"):
        respuesta_texto = None
        try:
            if accion["tipo"] in remisiones_handler.TIPOS_CORRECCION:
                respuesta_texto = await remisiones_handler.procesar_flujo_remision(
                    telefono=telefono, usuario_id=usuario_id, bodega_id=bodega_id,
                    contexto=contexto, accion=accion, texto=texto,
                    texto_normalizado=texto_normalizado)
                if respuesta_texto is MANEJADO:
                    return
            elif accion["tipo"] == "reporte_fecha":
                # Flujo "Reporte por fecha": fecha escrita (DD/MM/AAAA,
                # DD-MM-AAAA o DD-MM) o el botón rápido 📅 Hoy (llega como
                # 'inv_hoy_hoy', ya ruteado antes, pero por si llega aquí).
                if texto_normalizado == "inv_hoy_hoy":
                    contexto["accion_pendiente"] = {}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_reporte_diario(telefono, bodega_id, message)
                else:
                    fecha_parseada = parsear_fecha_colombiana(texto)
                    if not fecha_parseada:
                        respuesta_texto = ("No entendí la fecha. Usa el formato DD/MM/AAAA "
                                           "(ejemplo: 25/08/2026) o presiona el botón 'Hoy'.")
                    else:
                        if fecha_parseada > fecha_local_mensaje(message):
                            respuesta_texto = "Esa fecha es futura. Indica una fecha válida."
                        else:
                            contexto["accion_pendiente"] = {}
                            await guardar_contexto(usuario_id, contexto)
                            await enviar_reporte_diario(telefono, bodega_id, message,
                                                        fecha=fecha_parseada)
                            return
            elif accion["tipo"] in remisiones_handler.TIPOS_APROBACION:
                respuesta_texto = await remisiones_handler.procesar_flujo_remision(
                    telefono=telefono, usuario_id=usuario_id, bodega_id=bodega_id,
                    contexto=contexto, accion=accion, texto=texto,
                    texto_normalizado=texto_normalizado)
                if respuesta_texto is MANEJADO:
                    return
            elif accion["tipo"] == "movimientos_material":
                if texto.lower().strip() in {"cancelar", "salir", "menu"}:
                    contexto["accion_pendiente"] = {}
                    respuesta_texto = "Operación cancelada."
                else:
                    texto_buscado = texto.lower().strip()

                    # El catálogo se muestra como "1. Acero, 2. ..." (orden
                    # alfabético). Si el usuario envía un número (ej. "6"), se
                    # resuelve la posición 6 de esa misma lista ordenada.
                    materiales_ordenados = sorted(
                        inventario.catalogo_materiales.values(), key=lambda m: m.nombre
                    )
                    if texto_buscado.isdigit():
                        nombre_resuelto = resolver_entrada_material(
                            texto_buscado, [m.nombre for m in materiales_ordenados]
                        )
                        material_encontrado = (
                            inventario.obtener_material_por_nombre(nombre_resuelto)
                            if nombre_resuelto else None
                        )
                        _coincidencias_finalizadas = True
                    else:
                        _coincidencias_finalizadas = False

                    if not _coincidencias_finalizadas:
                        # Se buscan TODOS los materiales cuyo nombre contenga lo
                        # escrito; así, si hay varias coincidencias, no se adivina.
                        coincidencias = [
                            mat for mat in materiales_ordenados
                            if texto_buscado in mat.nombre.lower()
                        ]

                        # Sin coincidencias por subcadena: se intenta el buscador
                        # exacto/tolerante por si fue un error de tipeo o sinónimo.
                        if not coincidencias:
                            try:
                                posible = inventario.obtener_material_por_nombre(texto)
                                coincidencias = [posible] if posible else []
                            except Exception:
                                coincidencias = []

                        if len(coincidencias) == 1:
                            material_encontrado = coincidencias[0]
                        elif len(coincidencias) > 1:
                            # AMBIGUO: se pide al usuario que confirme con el
                            # número o el nombre exacto de la lista.
                            nombre_unicos = list(dict.fromkeys(m.nombre for m in coincidencias))
                            contexto["accion_pendiente"] = {
                                "tipo": "confirmar_material",
                                "candidatos": nombre_unicos,
                                "texto_buscado": texto,
                            }
                            await guardar_contexto(usuario_id, contexto)
                            lista = "\n".join(f"{i+1}. {n}" for i, n in enumerate(nombre_unicos))
                            await enviar_mensaje_whatsapp(
                                telefono,
                                f"Varios materiales coinciden con '{texto}'. "
                                f"Escribe el número del que quieres o el nombre exacto:\n\n{lista}",
                            )
                            return
                        else:
                            material_encontrado = None

                    if not material_encontrado:
                        respuesta_texto = f"No encontré el material '{texto}'. Intenta de nuevo o escribe *cancelar*."
                        contexto["accion_pendiente"] = {"tipo": "movimientos_material"}
                    else:
                        contexto["accion_pendiente"] = {"tipo": "movimientos_rango", "material": material_encontrado.nombre}
                        await guardar_contexto(usuario_id, contexto)
                        await enviar_botones_whatsapp(
                            telefono, f"¿Qué rango de fechas quieres ver para {material_encontrado.nombre}?",
                            [("todo", "Todo el historial"), ("rango", "Elegir fechas")],
                        )
                        return
            elif accion["tipo"] == "confirmar_material":
                candidatos = accion.get("candidatos", [])
                eleccion = texto.strip().lower()
                elegido = None
                if eleccion.isdigit():
                    idx = int(eleccion) - 1
                    if 0 <= idx < len(candidatos):
                        elegido = candidatos[idx]
                else:
                    # Coincidencia por nombre exacto dentro de la lista ofrecida.
                    for c in candidatos:
                        if c.lower() == eleccion:
                            elegido = c
                            break
                    if not elegido:
                        try:
                            mat = inventario.obtener_material_por_nombre(texto)
                            if mat and mat.nombre in candidatos:
                                elegido = mat.nombre
                        except Exception:
                            pass
                if elegido:
                    contexto["accion_pendiente"] = {"tipo": "movimientos_rango", "material": elegido}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_botones_whatsapp(
                        telefono, f"¿Qué rango de fechas quieres ver para {elegido}?",
                        [("todo", "Todo el historial"), ("rango", "Elegir fechas")],
                    )
                    return
                lista = "\n".join(f"{i+1}. {n}" for i, n in enumerate(candidatos))
                respuesta_texto = f"No reconocí '{texto}'. Escribe el número o el nombre exacto:\n\n{lista}"
            elif accion["tipo"] == "movimientos_rango":
                eleccion = texto.strip().lower()
                if eleccion in {"todo", "1"}:
                    resultado = await asyncio.to_thread(
                        inventario.obtener_movimientos_material,
                        bodega_id=bodega_id, material_nombre=accion["material"],
                    )
                    respuesta_texto = formatear_movimientos_material(resultado)
                    contexto["accion_pendiente"] = {}
                elif eleccion in {"rango", "2"}:
                    contexto["accion_pendiente"] = {"tipo": "movimientos_desde", "material": accion["material"]}
                    respuesta_texto = "Fecha desde (dd-mm-aaaa o dd-mm):"
                else:
                    respuesta_texto = "Responde 'todo' o 'rango', o usa los botones."
            elif accion["tipo"] == "movimientos_desde":
                fecha_desde = parsear_fecha_colombiana(texto)
                if not fecha_desde:
                    respuesta_texto = "No entendí la fecha. Usa el formato dd-mm-aaaa o dd-mm (ejemplo: 13-08-2026 o 13-08)."
                else:
                    contexto["accion_pendiente"] = {"tipo": "movimientos_hasta", "material": accion["material"], "fecha_desde": fecha_desde}
                    respuesta_texto = "Fecha hasta (dd-mm-aaaa o dd-mm):"
            elif accion["tipo"] == "movimientos_hasta":
                fecha_hasta = parsear_fecha_colombiana(texto)
                if not fecha_hasta:
                    respuesta_texto = "No entendí la fecha. Usa el formato dd-mm-aaaa o dd-mm (ejemplo: 18-08-2026 o 18-08)."
                else:
                    resultado = await asyncio.to_thread(
                        inventario.obtener_movimientos_material,
                        bodega_id=bodega_id, material_nombre=accion["material"],
                        fecha_desde=accion["fecha_desde"], fecha_hasta=fecha_hasta,
                    )
                    respuesta_texto = formatear_movimientos_material(resultado)
                    contexto["accion_pendiente"] = {}
            elif accion["tipo"] == "crear_menu":
                # Menú unificado de creación: 1 Cliente | 2 Conductor | 3 Material.
                eleccion = texto.strip().lower()
                if eleccion in {"1", "cliente", "crear_cliente"}:
                    contexto["accion_pendiente"] = {"tipo": "crear_cliente_bloque", "datos": {}}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_mensaje_whatsapp(
                        telefono,
                        "Envíame los datos del cliente (nombre, CC/NIT, celular, dirección).\n"
                        "Ejemplo:\n"
                        "Juan Pérez\nCC 1020304050\nCel 3001234567\nCalle 10 #5-20\n\n"
                        "O escribe *cancelar* para salir.",
                    )
                    return
                elif eleccion in {"2", "conductor", "crear_conductor"}:
                    contexto["accion_pendiente"] = {"tipo": "crear_conductor_bloque", "datos": {}}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_mensaje_whatsapp(
                        telefono,
                        "Envíame los datos del conductor (nombre, ID, Placa/Patente, celular).\n"
                        "Ejemplo:\n"
                        "Pedro Gómez\nCC 1098765432\nPlaca ABC123\nCel 3112345678\n\n"
                        "Si trae remolque, agrega su Patente (ej. 'Trailer BBB456').\n\n"
                        "O escribe *cancelar* para salir.",
                    )
                    return
                elif eleccion in {"3", "material", "producto", "producto / material", "crear_material"}:
                    await iniciar_creacion_material(telefono, usuario_id, contexto)
                    return
                else:
                    respuesta_texto = "Opción inválida. Escribe 1 (Cliente), 2 (Conductor) o 3 (Producto/Material)."
            elif accion["tipo"] == "cliente_duplicado_pendiente":
                # El usuario eligió "Usar este cliente" o "Ingresar otro".
                if texto in ("usar_cliente", "si", "sí", "usar ese", "1"):
                    # Confirma la selección: guarda el id en contexto y limpia.
                    contexto["cliente_id"] = accion.get("cliente_existente_id")
                    contexto["cliente_nombre"] = accion.get("cliente_existente_nombre")
                    contexto["accion_pendiente"] = {}
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_mensaje_whatsapp(
                        telefono,
                        f"✅ Cliente '{accion.get('cliente_existente_nombre')}' "
                        "seleccionado. Continúa con la operación.",
                    )
                    return
                if texto in ("otro_cliente", "otro", "2", "diferente"):
                    # Limpia todo el contexto y vuelve al menú principal.
                    contexto["accion_pendiente"] = {}
                    contexto["campo_esperado"] = None
                    await guardar_contexto(usuario_id, contexto)
                    await enviar_mensaje_whatsapp(
                        telefono,
                        "Operación cancelada. Puedes intentar de nuevo o "
                        "escribir *hola* para el menú principal.",
                    )
                    return
                # Cualquier otro texto vuelve a mostrar los botones.
                await enviar_botones_whatsapp(
                    telefono,
                    "Por favor selecciona una opción:",
                    [
                        ("usar_cliente", "Usar este cliente"),
                        ("otro_cliente", "Ingresar otro"),
                    ],
                )
                return

            elif accion["tipo"] in ("crear_cliente_bloque", "crear_cliente_paso"):
                respuesta_texto = await clientes_handler.procesar_flujo_cliente(
                    telefono=telefono, usuario_id=usuario_id, bodega_id=bodega_id,
                    contexto=contexto, accion=accion, texto=texto)
                if respuesta_texto is MANEJADO:
                    return
            elif accion["tipo"] in ("crear_conductor_bloque", "crear_conductor_paso"):
                respuesta_texto = await conductores_handler.procesar_flujo_conductor(
                    telefono=telefono, usuario_id=usuario_id, bodega_id=bodega_id,
                    contexto=contexto, accion=accion, texto=texto)
                if respuesta_texto is MANEJADO:
                    return
            elif accion["tipo"] == "crear_material_paso":
                respuesta_texto = await materiales_handler.procesar_flujo_material(
                    telefono=telefono, usuario_id=usuario_id, bodega_id=bodega_id,
                    contexto=contexto, accion=accion, texto=texto)
                if respuesta_texto is MANEJADO:
                    return
        except Exception as exc:
            contexto["accion_pendiente"] = {}
            respuesta_texto = f"No pude completar la acción: {exc}. Se canceló, intenta de nuevo."
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, respuesta_texto)
        return
    # Atajo de saludos: responde con la bienvenida solo si el usuario NO
    # está dentro de un wizard activo (sin accion_pendiente ni campo_esperado).
    # Así un "hola" durante el registro no aborta el flujo en curso.
    if (
        not contexto.get("accion_pendiente")
        and not contexto.get("campo_esperado")
        and texto_normalizado in SALUDOS_BIENVENIDA
    ):
        await enviar_mensaje_whatsapp(telefono, _MENSAJE_BIENVENIDA)
        return

    # Comandos directos por texto
    if texto.lower() in {"anular", "corregir", "anular/corregir rem", "anular rem", "corregir rem", "anular o corregir"}:
        contexto["accion_pendiente"] = {"tipo": "espera_remision_modo"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(
            telefono,
            "¿Deseas ANULAR o CORREGIR una remisión?",
            [("anular_rem", "Anular Remisión"), ("corregir_rem", "Corregir Remisión")],
        )
        return
    if texto.lower() == "corregir cliente":
        contexto["accion_pendiente"] = {"tipo": "correccion_cliente_nombre"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "¿Cuál cliente deseas corregir? (nombre)")
        return
    # Código directo de Orden de Salida (ej. "OS-1001", "os 1001", "OS_1001"):
    # omite el menú y entra directo a la valorización de esa orden.
    m_codigo_os = re.fullmatch(r"os[-_ ]?(\d{1,10})", texto_normalizado)
    if m_codigo_os:
        await iniciar_aprobacion_orden_salida(
            telefono, usuario_id, bodega_id, contexto,
            numero_directo=f"REM_{m_codigo_os.group(1)}",
        )
        return
    if texto_normalizado in TRIGGERS_ORDENES_SALIDA:
        await iniciar_aprobacion_orden_salida(telefono, usuario_id, bodega_id, contexto)
        return
    # Módulo unificado de creación: Cliente / Conductor / Producto-Material.
    if texto_normalizado in {"crear", "nuevo registro", "crear registro", "registrar", "crear nuevo"}:
        context = usuario.get("contexto_operacion") or {}
        context["codigo_crear"] = True
        # Sin borrador previo ni intento de selección: va directo al menú.
        await iniciar_creacion(telefono, usuario_id, context)
        return
    if texto.lower() in {"ver grafico", "ver gráfico", "reporte visual"}:
        url = await asyncio.to_thread(generar_y_subir_grafico_stock, bodega_id)
        if url:
            await enviar_imagen_whatsapp(telefono, url, f"Inventario de la bodega {bodega_id}")
        else:
            await enviar_mensaje_whatsapp(telefono, "No hay datos para generar el gráfico.")
        return
    # `texto_normalizado` ya viene normalizado desde el inicio de la función.
    if texto_normalizado in {"reporte de hoy", "reporte hoy", "ver reporte de hoy"}:
        await enviar_reporte_diario(telefono, bodega_id, message)
        return
    if texto_normalizado in {"reporte de ayer", "reporte ayer", "ver reporte de ayer"}:
        await enviar_reporte_diario(telefono, bodega_id, message, dias_atras=1)
        return
        

# Despliegue estricto de sub-botones al presionar o escribir "Ver Inventario"
    if texto_normalizado in {"ver_inventario", "ver inventario", "ver saldos", "saldos", "inventario", "2"}:
        contexto["borrador_pendiente"] = {}
        contexto["campo_esperado"] = None
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(telefono, "¿Qué deseas consultar en el inventario?", SUB_MENU_INVENTARIO)
        return

    # 2. Sub-botón: Inventario total
    if texto_normalizado == "inv_total":
        await iniciar_inventario_total(telefono, usuario_id, contexto)
        return

    # 2b. Sub-botones del submenú "Inventario Total" (informe texto / gráfico).
    if texto_normalizado == "inv_txt":
        await enviar_inventario_total(telefono, bodega_id)
        return
    if texto_normalizado == "inv_graf":
        await enviar_grafico_inventario(telefono, bodega_id)
        return

    # 3. Sub-botón: Ver movimientos
    if texto_normalizado == "inv_movs":
        await pedir_movimientos_material(telefono, usuario_id, contexto)
        return

    # 4. Sub-botón: Reporte de hoy
    if texto_normalizado == "inv_hoy":
        await iniciar_reporte_por_fecha(telefono, usuario_id, contexto)
        return

    # 4b. Botón rápido "📅 Hoy" del flujo Reporte por fecha.
    if texto_normalizado == "inv_hoy_hoy":
        await enviar_reporte_diario(telefono, bodega_id, message)
        return

    # Acceso directo por texto para movimientos
    if texto_normalizado in {"movimiento", "movimientos", "ver movimientos", "historial", "movimientos material"}:
        await pedir_movimientos_material(telefono, usuario_id, contexto)
        return

    # Opciones de Menú
    if texto in {"1", "Ingresar Inventario"}:
        contexto["campo_esperado"] = "menu_ingreso"
        await guardar_contexto(usuario_id, contexto)
        submenu_ingreso = [
            ("entrada", "Entrada"),
            ("arreglo", "Seleccion o Arreglo"),
            ("salida", "Salida o venta")
        ]
        await enviar_botones_whatsapp(
            telefono,
            "Selecciona el tipo de movimiento a registrar:",
            submenu_ingreso
        )
        return

    elif texto in {"3", "Anular Inventario"} or texto.lower() in {"anular", "corregir", "anular/corregir rem", "anular rem", "corregir rem", "anular o corregir", "anular/corregir"}:
        contexto["accion_pendiente"] = {"tipo": "espera_remision_modo"}
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(
            telefono,
            "¿Deseas ANULAR o CORREGIR una remisión?",
            [("anular_rem", "Anular Remisión"), ("corregir_rem", "Corregir Remisión")],
        )
        return

    
    await remisiones_handler.procesar_wizard_registro(
        message=message, texto=texto, texto_normalizado=texto_normalizado,
        telefono=telefono, usuario=usuario, usuario_id=usuario_id,
        bodega_id=bodega_id, contexto=contexto,
    )
