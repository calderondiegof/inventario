"""Handler de remisiones: wizard de anulacion/correccion, aprobacion de Ordenes de Salida y wizard de registro de Entrada/Salida."""
import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from core import config as _config
from core.config import BOGOTA, inventario, supabase
from core.contexto import fecha_local_mensaje, guardar_contexto
from core.whatsapp import (
    enviar_botones_whatsapp, enviar_documento_whatsapp, enviar_mensaje_whatsapp,
)
from core.modelos_ia import inferir_datos_ia, validar_completitud
from generador_pdf import generar_remision_pdf_archivo
from handlers import MANEJADO
from handlers.consultas_handler import (
    enviar_reporte_diario, iniciar_inventario_total, iniciar_reporte_por_fecha,
    pedir_movimientos_material,
)
from services.currency_service import obtener_tasa_dolar
from services.inventario_service import (
    es_lista_materiales, formatear_resumen_precios,
    parsear_bloque_persona, parsear_edicion_precio,
    procesar_precio_paso_a_paso,
)
from utils.parsers import (
    VENTA_CAMPOS_PASO, _parsear_numero, parsear_campos_cliente,
    parsear_campos_cliente_venta, parsear_fecha_colombiana,
    parsear_material_cantidad,
)
from utils.whatsapp_formatter import construir_mensaje_seleccion

logger = logging.getLogger(__name__)



TRIGGERS_ORDENES_SALIDA = {
    "ver ordenes de salida", "ordenes de salida",
    "aprobar remisiones", "aprobar remision",
    "aprobar ordenes", "aprobar ordenes de salida",
}
async def preparar_flujo_valorizacion(telefono: str, usuario_id: int, bodega_id: int,
                                      contexto: Dict[str, Any], remision: Dict[str, Any]) -> str:
    """Paso 2 del flujo de Contabilidad, compartido por el menú interactivo y
    el código directo: valida que la remisión sea una Orden de Salida aprobable,
    consulta la bodega/país, sugiere la tasa del dólar (currency_service) y
    prepara el contexto para el paso 3 (valor del dólar + precios por kilo).
    Devuelve el texto de respuesta para el usuario."""
    numero = remision["numero"]
    if (remision.get("estado") or "").upper() != "ORDEN_SALIDA":
        contexto["accion_pendiente"] = {}
        return (f"La remisión {numero} está en estado {remision.get('estado')}; "
                f"solo se aprueban Órdenes de Salida.")
    if not remision.get("movimientos"):
        contexto["accion_pendiente"] = {}
        return f"La Orden {numero} no tiene materiales registrados."
    # Info de la bodega: país/moneda para la tasa del dólar.
    bodegas = await asyncio.to_thread(lambda: supabase.table("bodegas")
                                      .select("*").eq("id", bodega_id).limit(1).execute())
    filas_b = getattr(bodegas, "data", None) or [{}]
    info = (filas_b[0] if filas_b else {}) or {}
    pais = info.get("pais") or "Colombia"
    moneda = info.get("moneda") or ""
    tasa = await obtener_tasa_dolar(pais=pais, moneda=moneda)
    tasa_txt = f"{tasa:,.2f}" if tasa else "no disponible (podrás escribirla manualmente)"
    items = [
        {"movimiento_id": m["id"],
         "material_nombre": (m.get("materiales") or {}).get("nombre", "Material"),
         "cantidad_kg": abs(float(m["cantidad_kg"]))}
        for m in remision["movimientos"]
    ]
    contexto["accion_pendiente"] = {
        "tipo": "espera_valor_dolar_dia",
        "remision_id": remision["id"],
        "numero": numero, "pais": pais, "moneda": moneda,
        "tasa_sugerida": tasa, "items": items, "precios": {},
    }
    await guardar_contexto(usuario_id, contexto)
    return (
        f"Orden de Salida {numero} — {len(items)} material(es).\n"
        f"💵 Tasa sugerida del dólar ({pais}): {tasa_txt}\n\n"
        f"Ingrese el Valor_dolar_dia a fijar en la remisión:"
    )


async def iniciar_aprobacion_orden_salida(telefono: str, usuario_id: int,
                                          bodega_id: int, contexto: Dict[str, Any],
                                          numero_directo: Optional[str] = None) -> None:
    """Flujo de Contabilidad, paso 1: consulta las últimas 3 remisiones en
    estado 'ORDEN_SALIDA' de la bodega y muestra el menú de selección con
    botones interactivos de WhatsApp.

    Si se pasa `numero_directo` (ej. 'REM_1001' desde el código 'OS-1001'),
    se omite el menú y se salta directo a la valorización de esa orden."""
    if numero_directo:
        remision = await asyncio.to_thread(inventario.obtener_remision, numero_directo)
        if not remision:
            await enviar_mensaje_whatsapp(
                telefono, f"No encontré la Orden '{numero_directo}'. "
                          f"Escribe 'ver ordenes de salida' para ver las pendientes.")
            return
        logger.info(f"Código directo de Orden de Salida: {numero_directo} "
                    f"(remision_id={remision['id']}, estado={remision.get('estado')})")
        respuesta = await preparar_flujo_valorizacion(telefono, usuario_id, bodega_id, contexto, remision)
        await enviar_mensaje_whatsapp(telefono, respuesta)
        return
    ordenes = await asyncio.to_thread(inventario.obtener_ordenes_salida, bodega_id, 3)
    logger.info(f"Resultado consulta Supabase remisiones (ORDEN_SALIDA, bodega={bodega_id}): {ordenes}")
    if not ordenes:
        await enviar_mensaje_whatsapp(
            telefono, "No hay Órdenes de Salida pendientes de valoración en tu bodega.")
        return
    contexto["accion_pendiente"] = {
        "tipo": "orden_salida_menu",
        "ordenes": [{"remision_id": o["id"], "numero": o["numero"]} for o in ordenes],
    }
    await guardar_contexto(usuario_id, contexto)
    lista = "\n".join(f"• {o['numero']} — fecha {o.get('fecha_operacion') or 'n/d'}" for o in ordenes)
    await enviar_botones_whatsapp(
        telefono,
        f"Órdenes de Salida pendientes de valoración:\n\n{lista}\n\nSelecciona la que deseas aprobar:",
        [(o["numero"], o["numero"]) for o in ordenes[:3]],
    )
def consolidar_seleccion(datos: Dict[str, Any], texto: str) -> None:
    """Post-procesado del borrador de SELECCION_REVUELTO (muta `datos`):

    1. Si el mensaje del usuario es una LISTA de materiales (viñetas o líneas
       'Material Cantidad'), se resuelve de forma DETERMINISTA con
       resolver_lista_materiales: cada línea se consume una vez y los ítems no
       encontrados se reportan en datos['materiales_omitidos'] (nada se
       omite en silencio, evitando que la IA simplifique/recorte frases).
    2. Clasificación de merma: cualquier item cuyo material sea de tipo MERMA
       (ej. 'Basura') se mueve a merma_kg — la basura nunca se registra como
       material comercializable.

    Debe llamarse con contexto de catálogo cargado (inventario.catalogo_materiales).
    """
    if datos.get("intencion") != "SELECCION_REVUELTO":
        return
    omitidos: List[str] = []
    if es_lista_materiales(texto):
        items, no_encontrados, merma_lista = inventario.resolver_lista_materiales(texto)
        datos["items"] = items
        datos["merma_kg"] = float(datos.get("merma_kg") or 0) + merma_lista
        omitidos = list(no_encontrados)
    else:
        # Ruta IA: separar items de tipo MERMA hacia merma_kg.
        items = datos.get("items") or []
        vendibles: List[Dict[str, Any]] = []
        for it in items:
            mat = inventario.obtener_material_por_nombre(it.get("material_nombre") or "")
            if mat and (mat.tipo_material or "").upper() == "MERMA":
                datos["merma_kg"] = float(datos.get("merma_kg") or 0) + float(it.get("cantidad_kg") or 0)
            else:
                vendibles.append(it)
        datos["items"] = vendibles
    datos["materiales_omitidos"] = omitidos
async def regenerar_y_enviar_pdf_remision(telefono: str, bodega_id: int, numero: str) -> str:
    """Regenera el PDF de una remisión EXISTENTE conservando el mismo número
    correlativo (no se genera uno nuevo) y lo envía por WhatsApp.

    Devuelve un mensaje de confirmación o de error.
    """
    datos = await asyncio.to_thread(inventario.obtener_datos_pdf_remision, numero)
    numero_remision = datos["numero_remision"]
    try:
        nombre_pdf = f"remision_{numero_remision}_{int(datetime.now(BOGOTA).timestamp())}.pdf"
        pdf_path = os.path.join(tempfile.gettempdir(), nombre_pdf)
        cliente = datos.get("cliente") or {}
        conductor = datos.get("conductor") or {}
        await asyncio.to_thread(
            generar_remision_pdf_archivo,
            pdf_path,
            fecha=datos.get("fecha_operacion") or "",
            cliente=cliente.get("nombre", "") or "",
            documento=cliente.get("identificacion"),
            direccion=cliente.get("direccion"),
            celular=cliente.get("telefono"),
            placa=conductor.get("placa"),
            conductor=conductor.get("nombre"),
            id_conductor=conductor.get("identificacion"),
            celular_conductor=conductor.get("telefono"),
            items=datos.get("items", []),
            numero_remision=numero_remision,
            bodega_id=bodega_id,
            # El PDF se adapta al estado: 'ORDEN_SALIDA' sin precios,
            # 'APROBADA' completo con conversiones a dólar.
            estado=datos.get("estado"),
            vr_dolar_dia=datos.get("vr_dolar_dia"),
        )
        await enviar_documento_whatsapp(
            destino=telefono,
            ruta_archivo=pdf_path,
            nombre_documento=f"Remision_Corregida_{numero_remision}.pdf",
        )
    except Exception as e:
        logger.error(f"❌ Error regenerando PDF de {numero_remision}: {e}")
        raise
    return f"✅ Remisión {numero_remision} corregida. PDF regenerado con el mismo número y enviado por WhatsApp."


# Tipos de accion_pendiente que gestiona este handler.
TIPOS_CORRECCION = {
    "espera_remision_modo", "espera_numero_remision", "espera_alcance",
    "espera_material", "espera_confirmacion_actualizacion", "corregir_opciones",
    "correccion_rem_cliente", "correccion_cliente_nombre", "correccion_cliente_datos",
}
TIPOS_APROBACION = {
    "orden_salida_menu", "espera_valor_dolar_dia", "captura_precio_material",
    "resumen_precios", "seleccion_modo_pdf",
}


async def procesar_flujo_remision(telefono: str, usuario_id: int, bodega_id: int,
                                  contexto: Dict[str, Any], accion: Dict[str, Any],
                                  texto: str, texto_normalizado: str):
    """Wizard de remisiones (anulacion/correccion y aprobacion contabilidad).

    Devuelve el texto de respuesta o MANEJADO si el handler ya notifico."""
    if accion["tipo"] == "espera_remision_modo":
        eleccion = texto.strip().lower()
        if eleccion in {"anular", "anular_rem", "anular rem", "anulacion", "anulación", "1"}:
            contexto["accion_pendiente"] = {"tipo": "espera_numero_remision", "modo": "anular"}
            return "Anulación. ¿Qué remisión deseas anular? (ejemplo: REM_112)"
        elif eleccion in {"corregir", "corregir_rem", "corregir rem", "correccion", "corrección", "2"}:
            contexto["accion_pendiente"] = {"tipo": "espera_numero_remision", "modo": "corregir"}
            return "Corrección. ¿Qué remisión deseas corregir? (ejemplo: REM_112)"
        else:
            return "Responde 'anular' o 'corregir', o usa los botones."
    elif accion["tipo"] == "espera_numero_remision":
        remision = await asyncio.to_thread(inventario.obtener_remision, texto)
        if not remision:
            return f"No encontré la remisión '{texto}'. Verifica el número."
            contexto["accion_pendiente"] = {}
        elif accion.get("modo") == "corregir":
            contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": remision["numero"], "modo": "corregir"}
            return (
                f"Corrección de la remisión {remision['numero']}. ¿Qué deseas corregir?\n"
                "1. Material (cantidad)\n2. Cliente\n3. Finalizar y generar PDF"
            )
        else:
            contexto["accion_pendiente"] = {"tipo": "espera_alcance", "numero": remision["numero"], "modo": "anular"}
            return f"¿Deseas anular TODA la remisión {remision['numero']}? (sí/no)"
    elif accion["tipo"] == "espera_alcance":
        if texto.strip().lower() in {"si", "sí"}:
            r = await asyncio.to_thread(inventario.anular_remision_completa, accion["numero"], usuario_id)
            return f"Remisión {r['numero']} anulada por completo ({r['lineas_anuladas']} línea(s)). El stock fue devuelto."
            contexto["accion_pendiente"] = {}
        elif texto.strip().lower() == "no":
            contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
            return (
                f"Corrección de la remisión {accion['numero']}. ¿Qué deseas corregir?\n"
                "1. Material (cantidad)\n2. Cliente\n3. Finalizar y generar PDF"
            )
        else:
            return "Responde sí o no, por favor."
    elif accion["tipo"] == "espera_material":
        if texto.strip().lower() in {"listo", "terminar", "finalizar", "fin", "generar pdf", "finalizar y generar pdf"}:
            try:
                return await regenerar_y_enviar_pdf_remision(telefono, bodega_id, accion["numero"])
            except Exception as exc:
                return f"Correcciones guardadas, pero no se pudo regenerar el PDF: {exc}"
            contexto["accion_pendiente"] = {}
        else:
            par = parsear_material_cantidad(texto)
            if not par:
                return "No entendí. Escribe así: Material cantidad (ejemplo: Carter 3500). O escribe *finalizar* cuando termines."
            else:
                material_nombre, cantidad = par
                try:
                    r = await asyncio.to_thread(
                        inventario.anular_o_actualizar_linea, numero=accion["numero"],
                        material_nombre=material_nombre, cantidad_kg=cantidad, usuario_id=usuario_id,
                    )
                except ValueError as exc:
                    return str(exc)
                    contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": accion["numero"], "modo": "corregir"}
                else:
                    if r["accion"] == "anulada":
                        return (f"Se anuló {r['material']} ({r['cantidad']:,.2f} kg) de la remisión {accion['numero']}. "
                                           "Stock devuelto. Puedes seguir corrigiendo o escribe *finalizar*.")
                        contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
                    else:
                        contexto["accion_pendiente"] = {
                            "tipo": "espera_confirmacion_actualizacion", "numero": accion["numero"],
                            "movimiento_id": r["movimiento_id"], "material": r["material"],
                            "cantidad_nueva": r["cantidad_nueva"],
                        }
                        return (
                            f"Ese detalle no existe. En la remisión {accion['numero']}, {r['material']} está en "
                            f"{r['cantidad_actual']:,.2f} kg. ¿Deseas actualizarlo a {r['cantidad_nueva']:,.2f} kg? (sí/no)"
                        )
    elif accion["tipo"] == "espera_confirmacion_actualizacion":
        if texto.strip().lower() in {"si", "sí"}:
            await asyncio.to_thread(
                inventario.actualizar_cantidad_linea,
                movimiento_id=accion["movimiento_id"], nueva_cantidad_kg=accion["cantidad_nueva"],
            )
            return (f"{accion['material']} actualizado a {accion['cantidad_nueva']:,.2f} kg en la remisión {accion['numero']}. "
                               "Puedes seguir corrigiendo (Material/Cliente) o escribe *finalizar*.")
            contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
        elif texto.strip().lower() == "no":
            contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": accion["numero"], "modo": "corregir"}
            return "Digite los datos que desea modificar, o escribe *finalizar*."
        else:
            return "Responde sí o no, por favor."
    elif accion["tipo"] == "corregir_opciones":
        eleccion = texto.strip().lower()
        numero = accion["numero"]
        if eleccion in {"1", "material", "materiales"}:
            contexto["accion_pendiente"] = {"tipo": "espera_material", "numero": numero, "modo": "corregir"}
            return f"Corrección de materiales de {numero}. Escribe: Material cantidad (ejemplo: Carter 3500). O *finalizar* para terminar."
        elif eleccion in {"2", "cliente"}:
            rem = await asyncio.to_thread(inventario.obtener_remision, numero)
            if not rem or not rem.get("cliente_id"):
                return f"La remisión {numero} no tiene un cliente asociado. Elige otra opción."
                contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": numero, "modo": "corregir"}
            else:
                contexto["accion_pendiente"] = {
                    "tipo": "correccion_rem_cliente", "numero": numero,
                    "cliente_id": rem["cliente_id"], "modo": "corregir",
                }
                return "Escribe los datos del cliente a corregir (ejemplo: telefono 3001234567, direccion Calle 10 #5-20)."
        elif eleccion in {"3", "finalizar", "listo", "terminar", "fin", "generar pdf"}:
            try:
                return await regenerar_y_enviar_pdf_remision(telefono, bodega_id, numero)
            except Exception as exc:
                return f"Correcciones guardadas, pero no se pudo regenerar el PDF: {exc}"
            contexto["accion_pendiente"] = {}
        else:
            return "Elige: 1. Material, 2. Cliente, 3. Finalizar."
    elif accion["tipo"] == "correccion_rem_cliente":
        campos = parsear_campos_cliente(texto)
        if not campos:
            return "No entendí los datos. Ejemplo: telefono 3001234567, direccion Calle 10 #5-20."
        else:
            try:
                await asyncio.to_thread(inventario.actualizar_cliente, accion["cliente_id"], campos)
            except Exception as exc:
                return str(exc)
                contexto["accion_pendiente"] = {"tipo": "correccion_rem_cliente", "numero": accion["numero"], "cliente_id": accion["cliente_id"], "modo": "corregir"}
            else:
                return (f"Datos del cliente actualizados en la remisión {accion['numero']}. "
                                   "Puedes seguir corrigiendo (Material/Cliente) o escribe *finalizar*.")
                contexto["accion_pendiente"] = {"tipo": "corregir_opciones", "numero": accion["numero"], "modo": "corregir"}
    elif accion["tipo"] == "correccion_cliente_nombre":
        cliente = await asyncio.to_thread(inventario.obtener_cliente_por_nombre, texto)
        if not cliente:
            return f"No encontré ningún cliente llamado '{texto}'."
            contexto["accion_pendiente"] = {}
        else:
            contexto["accion_pendiente"] = {"tipo": "correccion_cliente_datos", "cliente_id": cliente["id"], "cliente_nombre": cliente["nombre"]}
            return "Escriba los datos que desea corregir (ejemplo: telefono 3001234567, direccion Calle 10 #5-20)."
    elif accion["tipo"] == "correccion_cliente_datos":
        campos = parsear_campos_cliente(texto)
        if not campos:
            return "No entendí los datos. Ejemplo: telefono 3001234567, direccion Calle 10 #5-20."
        else:
            await asyncio.to_thread(inventario.actualizar_cliente, accion["cliente_id"], campos)
            return f"Datos de {accion['cliente_nombre']} actualizados."
            contexto["accion_pendiente"] = {}
    elif accion["tipo"] == "orden_salida_menu":
        # Paso 2: el usuario (Contabilidad) selecciona una Orden de Salida
        # (por botón interactivo —llega su id, que es el número— o tipeándolo).
        # obtener_remision es tolerante a variantes: '101', 'REM_101',
        # 'OS-1001', 'os 1001', etc.
        remision = await asyncio.to_thread(inventario.obtener_remision, texto.strip())
        if not remision:
            return f"No encontré la Orden '{texto.strip()}'. Escribe el número o usa los botones."
        else:
            return await preparar_flujo_valorizacion(
                telefono, usuario_id, bodega_id, contexto, remision)
    elif accion["tipo"] == "espera_valor_dolar_dia":
        # Paso 3: captura del valor del dólar del día.
        valor = _parsear_numero(texto)
        if valor is None or valor <= 0:
            return ("Valor inválido. Ingrese el valor del dólar del día "
                               "(ejemplo: 4120,50) o escriba 'cancelar'.")
        else:
            accion["vr_dolar_dia"] = valor
            accion["tipo"] = "captura_precio_material"
            accion["indice"] = 1
            primer = accion["items"][0]
            return (
                f"Valor del dólar fijado: {valor:,.2f}\n\n"
                f"Ingrese el precio por kilo (en moneda local) para "
                f"{primer['material_nombre']} ({primer['cantidad_kg']:,.2f} kg):"
            )
    elif accion["tipo"] == "captura_precio_material":
        # Paso 4: bucle de precios por kilo, ítem por ítem.
        # - Si el usuario escribe "0" se DESCARTA el precio del material
        #   anterior y se vuelve a solicitar (corrección ágil).
        # - Al terminar se muestra el resumen enumerado editable.
        res = procesar_precio_paso_a_paso(
            texto, accion["items"], accion["precios"], accion.get("indice", 0))
        accion["precios"] = res["precios"]
        accion["indice"] = res["indice"]
        if res["tipo"] == "corregir":
            return res["texto"]
        elif res["tipo"] == "invalido":
            # Defensa contra respuestas vacías: si por algún motivo el texto
            # del rechazo llegara en blanco, regeneramos un mensaje de
            # re-pregunta con el material en curso. Evita que la API de
            # Meta rechace la petición con "text.body is required".
            txt = res.get("texto") or ""
            if not txt:
                idx = res.get("indice", 1)
                item = accion["items"][idx - 1] if 1 <= idx <= len(accion["items"]) else {}
                nombre = item.get("material_nombre", "el material")
                txt = (f"⚠️ Precio inválido. Indica el valor numérico por kilo "
                       f"para '{nombre}' (ej. 16000) o *0* para saltar:")
            return txt
        elif res["indice"] < len(accion["items"]):
            sig = accion["items"][res["indice"]]
            return (
                f"{res['texto']}\n\n"
                f"Ingrese el precio por kilo (en moneda local) para "
                f"{sig['material_nombre']} ({sig['cantidad_kg']:,.2f} kg):"
            )
        else:
            # Último precio capturado -> resumen final editable.
            accion["tipo"] = "resumen_precios"
            return formatear_resumen_precios(accion["items"], accion["precios"])
    elif accion["tipo"] == "resumen_precios":
        # Resumen final: OK/SI procesa; "[n] [precio]" corrige; 0/cancelar anula.
        eleccion = texto.strip().lower()
        if eleccion in {"cancelar", "0"}:
            contexto["accion_pendiente"] = {}
            return ("Operación de aprobación anulada. "
                               "Los precios no fueron guardados.")
        elif eleccion in {"ok", "si"}:
            accion["tipo"] = "seleccion_modo_pdf"
            return (
                "¿Cómo deseas que se impriman los valores en el PDF de la Remisión?\n"
                "1. Moneda local\n2. Moneda dólares\n3. Ambas monedas\n4. Sin valores"
            )
        else:
            edit = parsear_edicion_precio(texto)
            if edit:
                num_idx, precio_nuevo = edit
                if 1 <= num_idx <= len(accion["items"]):
                    item = accion["items"][num_idx - 1]
                    accion["precios"][str(item["movimiento_id"])] = precio_nuevo
                    return (
                        f"✅ Ítem {num_idx} ({item['material_nombre']}) actualizado "
                        f"a {precio_nuevo:,.2f}/kg.\n\n"
                        + formatear_resumen_precios(accion["items"], accion["precios"])
                    )
                else:
                    return (
                        f"Ítem fuera de rango (1-{len(accion['items'])}). "
                        f"Usa el formato *[número] [nuevo_precio]* (ej. *2 16700*)."
                    )
            else:
                return (
                    "No entendí. Escribe *OK*/*SI* para procesar la orden, "
                    "*[número] [nuevo_precio]* (ej. *2 16700*) para corregir un ítem, "
                    "o *0*/*CANCELAR* para anular."
                )
    elif accion["tipo"] == "seleccion_modo_pdf":
        # Paso 5: elección del formato de valores del PDF final y
        # aprobación de la remisión (RPC) + envío del PDF por WhatsApp.
        modos = {
            "1": "MONEDA_LOCAL", "moneda local": "MONEDA_LOCAL",
            "2": "DOLARES", "dolares": "DOLARES", "dólares": "DOLARES",
            "moneda dolares": "DOLARES", "moneda dólares": "DOLARES",
            "3": "AMBAS", "ambas": "AMBAS", "ambas monedas": "AMBAS",
            "4": "SIN_VALORES", "sin valores": "SIN_VALORES",
        }
        modo = modos.get(texto.strip().lower())
        if not modo:
            return ("Opción inválida. Responde 1 (Moneda local), 2 (Moneda dólares), "
                               "3 (Ambas monedas) o 4 (Sin valores).")
        else:
            await asyncio.to_thread(
                inventario.aprobar_remision_con_precios,
                accion["remision_id"], accion["vr_dolar_dia"],
                # Claves tal cual (str en BD real con UUID; el servicio
                # las normaliza a str para el JSONB). NO convertir a
                # int: con PKs UUID lanzaría ValueError.
                dict(accion["precios"]),
            )
            datos_pdf = await asyncio.to_thread(
                inventario.obtener_datos_pdf_remision, accion["numero"])
            total_local = sum(
                float(i.get("cantidad_kg") or 0) * float(i.get("precio_unitario") or 0)
                for i in datos_pdf.get("items", [])
            )
            total_dolar = total_local / accion["vr_dolar_dia"] if accion["vr_dolar_dia"] else 0.0
            numero_rem = datos_pdf.get("numero_remision") or accion["numero"]
            etiqueta_modo = {
                "MONEDA_LOCAL": "Moneda local",
                "DOLARES": "Moneda dólares",
                "AMBAS": "Ambas monedas",
                "SIN_VALORES": "Sin valores",
            }[modo]
            try:
                nombre_pdf = f"remision_aprobada_{usuario_id}_{int(datetime.now(BOGOTA).timestamp())}.pdf"
                pdf_path = os.path.join(tempfile.gettempdir(), nombre_pdf)
                cliente = datos_pdf.get("cliente") or {}
                conductor = datos_pdf.get("conductor") or {}
                await asyncio.to_thread(
                    generar_remision_pdf_archivo,
                    pdf_path,
                    fecha=datos_pdf.get("fecha_operacion") or "",
                    cliente=cliente.get("nombre", "") or "",
                    documento=cliente.get("identificacion"),
                    direccion=cliente.get("direccion"),
                    celular=cliente.get("telefono"),
                    placa=conductor.get("placa"),
                    conductor=conductor.get("nombre"),
                    id_conductor=conductor.get("identificacion"),
                    celular_conductor=conductor.get("telefono"),
                    items=datos_pdf.get("items", []),
                    numero_remision=numero_rem,
                    bodega_id=bodega_id,
                    estado="APROBADA",
                    vr_dolar_dia=accion["vr_dolar_dia"],
                    modo_valores=modo,
                )
                logger.info(f"📄 Remisión Aprobada generada ({modo}): {pdf_path}")
                await enviar_documento_whatsapp(
                    destino=telefono,
                    ruta_archivo=pdf_path,
                    nombre_documento=f"Remision_Aprobada_{numero_rem}.pdf",
                )
                return (
                    f"✅ Remisión Aprobada #{numero_rem}.\n"
                    f"💵 Valor dólar día: {accion['vr_dolar_dia']:,.2f}\n"
                    f"🧾 Total valorizado: {total_local:,.2f} {accion.get('moneda') or ''}"
                    f" (≈ US$ {total_dolar:,.2f})\n"
                    f"🖨️ PDF generado en modo: {etiqueta_modo}."
                )
            except Exception as e:
                logger.error(f"❌ Error generando/enviando PDF de Remisión Aprobada: {e}")
                return (
                    f"✅ Remisión {numero_rem} APROBADA y precios guardados "
                    f"({len(accion['precios'])} precio(s)).\n"
                    f"⚠️ No se pudo generar o enviar el PDF. Intenta corregir la remisión "
                    f"para regenerarlo."
                )
            contexto["accion_pendiente"] = {}


async def procesar_wizard_registro(message: Dict[str, Any], texto: str, texto_normalizado: str,
                                telefono: str, usuario: Dict[str, Any], usuario_id: int,
                                bodega_id: int, contexto: Dict[str, Any]) -> None:
    """Wizard completo de registro de Entrada/Salida (remision): borrador,
    interpretacion por IA, consolidacion, registro en DB y PDF."""
    await asyncio.to_thread(inventario.recargar_catalogos)
    fecha_mensaje = fecha_local_mensaje(message)
    borrador_anterior = contexto.get("borrador_pendiente") or {}
    campo_esperado = contexto.get("campo_esperado")

    if campo_esperado in VENTA_CAMPOS_PASO:
        datos = dict(borrador_anterior)
        clave = VENTA_CAMPOS_PASO[campo_esperado]
        valor = texto.strip()
        # Unificación con el módulo Crear: en los pasos de Nombre de Cliente y
        # Nombre de Conductor, si el usuario envía el DATO EN BLOQUE (varias
        # líneas: nombre + CC + tel + dirección o + placa), se parsea con el
        # MISMO parser del módulo Crear (parsear_bloque_persona) y se llenan
        # todos los campos extraíbles del borrador. Si no es un bloque, se usa
        # el mecanismo clásico (etiquetas 'placa:', 'id:' o valores por coma).
        if clave in ("cliente", "cliente_conductor"):
            bloque = parsear_bloque_persona(texto)
            if bloque.get("nombre"):
                datos[clave] = bloque["nombre"]
                if clave == "cliente":
                    datos["cliente_documento"] = bloque.get("identificacion") or datos.get("cliente_documento")
                    datos["cliente_celular"] = bloque.get("telefono") or datos.get("cliente_celular")
                    datos["cliente_direccion"] = bloque.get("direccion") or datos.get("cliente_direccion")
                else:
                    datos["cliente_conductor_id"] = bloque.get("identificacion") or datos.get("cliente_conductor_id")
                    datos["cliente_conductor_celular"] = bloque.get("telefono") or datos.get("cliente_conductor_celular")
                    datos["cliente_placa"] = bloque.get("placa") or datos.get("cliente_placa")
            else:
                campos_parseados = parsear_campos_cliente_venta(texto)
                if clave in campos_parseados:
                    datos[clave] = campos_parseados[clave]
                else:
                    datos[clave] = valor
        else:
            # Si el usuario incluye la etiqueta del paso (ej. "placa ABC123",
            # "id 1098") se extrae solo el valor mediante el parser de campos;
            # si no, se toma el texto tal cual.
            campos_parseados = parsear_campos_cliente_venta(texto)
            if clave in campos_parseados:
                valor = campos_parseados[clave]
            datos[clave] = valor
    elif campo_esperado in {"tipo_movimiento", "menu_ingreso"}:
        datos = dict(borrador_anterior)
        eleccion = texto.strip().lower()
        if eleccion in {"1", "entrada"}:
            datos["intencion"] = "AJUSTE_INVENTARIO"
        elif eleccion in {"2", "arreglo", "transformacion", "transformación", "seleccion", "selección", "seleccion arreglo"}:
            datos["intencion"] = "SELECCION_REVUELTO"
        elif eleccion in {"3", "salida", "venta", "despacho"}:
            datos["intencion"] = "VENTA_DESPACHO"
        else:
            await enviar_mensaje_whatsapp(telefono, "No entendí. Selecciona: Entrada, Seleccion Arreglo o Salida.")
            return MANEJADO
    elif campo_esperado == "fecha_operacion":
        datos = dict(borrador_anterior)
        fecha_parseada = parsear_fecha_colombiana(texto)
        if fecha_parseada:
            if fecha_parseada > fecha_mensaje:
                await enviar_mensaje_whatsapp(telefono, "Esa fecha es futura. Indica una fecha válida.")
                return MANEJADO
            datos["fecha_operacion"] = fecha_parseada
        else:
            datos = await inferir_datos_ia(usuario, bodega_id, fecha_mensaje, borrador_anterior, texto)
    else:
        datos = await inferir_datos_ia(usuario, bodega_id, fecha_mensaje, borrador_anterior, texto)
    if datos is None:
        # IA no disponible: se purgan los materiales del borrador para que un
        # reintento del usuario no acumule ítems del intento fallido.
        if isinstance(contexto.get("borrador_pendiente"), dict):
            contexto["borrador_pendiente"]["items"] = []
            await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, "No pude interpretar el mensaje. Inténtalo nuevamente.")
        return MANEJADO
    # Consolidación de la selección: resolución determinista de listas (nada
    # se omite en silencio) y clasificación de merma (Basura → merma_kg).
    consolidar_seleccion(datos, texto)

    cliente_existente = None
    conductor_existente = None
    if datos.get("intencion") == "VENTA_DESPACHO" and datos.get("cliente"):
        cliente_existente = await asyncio.to_thread(inventario.obtener_cliente_por_nombre, datos["cliente"])
        if cliente_existente:
            datos["cliente_documento"] = datos.get("cliente_documento") or cliente_existente.get("identificacion")
            datos["cliente_direccion"] = datos.get("cliente_direccion") or cliente_existente.get("direccion")
            datos["cliente_celular"] = datos.get("cliente_celular") or cliente_existente.get("telefono")
    if datos.get("intencion") == "VENTA_DESPACHO" and datos.get("cliente_conductor"):
        conductor_existente = await asyncio.to_thread(inventario.obtener_conductor_por_nombre, datos["cliente_conductor"])
        if conductor_existente:
            # Si el conductor ya existe, completar los datos que falten a partir del
            # registro: así solo se piden por pasos los campos que aún no tiene.
            datos["cliente_conductor_id"] = datos.get("cliente_conductor_id") or conductor_existente.get("identificacion")
            datos["cliente_placa"] = datos.get("cliente_placa") or conductor_existente.get("placa")
            datos["cliente_conductor_celular"] = datos.get("cliente_conductor_celular") or conductor_existente.get("telefono")

    if datos.get("intencion") in (None, "OTRO") and datos.get("items"):
        contexto["borrador_pendiente"] = datos
        contexto["campo_esperado"] = "tipo_movimiento"
        await guardar_contexto(usuario_id, contexto)
        await enviar_botones_whatsapp(
            telefono, "Selecciona el tipo de movimiento:",
            [("entrada", "Entrada"), ("arreglo", "Seleccion Arreglo"), ("salida", "Salida")],
        )
        return MANEJADO
    resultado_validacion = validar_completitud(datos, fecha_mensaje, cliente_existe=bool(cliente_existente))
    if resultado_validacion:
        mensaje_faltante, campo = resultado_validacion
        contexto["borrador_pendiente"] = datos
        contexto["campo_esperado"] = campo
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, mensaje_faltante)
        return MANEJADO
    # PROTECCIÓN 1 (estado): el borrador se purga ANTES del guardado final.
    # Si llega un duplicado/reintento mientras se escribe en DB, el contexto
    # ya no contiene el payload y la segunda pasada no re-registra nada.
    contexto["campo_esperado"] = None
    contexto["borrador_pendiente"] = {}
    await guardar_contexto(usuario_id, contexto)
    try:
        intencion, fecha = datos["intencion"], datos.get("fecha_operacion", fecha_mensaje)
        if intencion == "CONSULTA":
            material = inventario.obtener_material_por_nombre(datos.get("consulta_material") or "")
            if material:
                salida = f"Stock de {material.nombre}: {inventario.obtener_saldo(bodega_id, material.id):,.2f} kg."
            else:
                saldos = inventario.obtener_saldos_bodega(bodega_id)
                salida = "\n".join(["Inventario actual:"] + [f"- {x['material']}: {x['saldo_kg']} kg" for x in saldos])
        elif intencion == "CONSULTA_INVENTARIO_TOTAL":
            await iniciar_inventario_total(telefono, usuario_id, contexto)
            return MANEJADO
        elif intencion == "VER_MOVIMIENTOS_SELECCION":
            await pedir_movimientos_material(telefono, usuario_id, contexto)
            return MANEJADO
        elif intencion == "REPORTE_POR_FECHA":
            fecha_rep = datos.get("fecha_operacion")
            if fecha_rep:
                await enviar_reporte_diario(telefono, bodega_id, message, fecha=str(fecha_rep))
            else:
                await iniciar_reporte_por_fecha(telefono, usuario_id, contexto)
            return MANEJADO
        elif intencion == "REGISTRO_DIARIO":
            r = await asyncio.to_thread(inventario.registrar_registro_diario, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, entradas=datos.get("entradas_revuelto", []), resultados=datos.get("items", []), merma_kg=datos.get("merma_kg", 0), cantidad_revuelto_procesada=datos.get("cantidad_revuelto_procesada"))
            salida = (f"⚠️ Registro diario duplicado detectado; ya se había guardado hace instantes (merma {r['merma_kg']:,.2f} kg, fecha {fecha})."
                      if r.get("duplicado") else
                      f"Registro diario guardado: {len(r['registros'])} movimientos y merma de {r['merma_kg']:,.2f} kg, fecha {fecha}.")
        elif intencion == "ENTRADA_REVUELTO":
            r = await asyncio.to_thread(inventario.registrar_entrada_revuelto, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, entradas=datos.get("entradas_revuelto", []))
            salida = f"Entrada de Revuelto registrada: {len(r['registros'])} fuente(s), fecha {fecha}."
        elif intencion == "SELECCION_REVUELTO":
            r = await asyncio.to_thread(inventario.registrar_seleccion_revuelto, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, resultados=datos.get("items", []), merma_kg=datos.get("merma_kg", 0), cantidad_revuelto_procesada=datos.get("cantidad_revuelto_procesada"))
            salida = ("⚠️ Selección duplicada: ya se registró hace instantes "
                      f"(revuelto: -{r['revuelto_descontado']:g} kg, fecha {fecha}); no se volvió a registrar."
                      if r.get("duplicado") else
                      construir_mensaje_seleccion(r, fecha, datos.get("materiales_omitidos")))
        elif intencion == "TRANSFORMACION_MATERIAL":
            origen = datos.get("material_origen") or "Revuelto"
            r = await asyncio.to_thread(
                inventario.registrar_transformacion_material,
                bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha,
                material_origen_nombre=origen,
                resultados=datos.get("items", []),
                merma_kg=datos.get("merma_kg", 0),
                cantidad_procesada=datos.get("cantidad_revuelto_procesada"),
                material_merma_nombre=datos.get("material_merma"),
                nombre_proceso=datos.get("nombre_proceso") or "Transformación",
            )
            salida = ("⚠️ Transformación duplicada: ya se registró hace instantes; no se volvió a registrar."
                      if r.get("duplicado") else
                      (f"Transformación registrada desde {r['origen']}: {len(r['registros'])} movimiento(s), "
                       f"merma {r['merma_kg']:,.2f} kg, fecha {fecha}."))
        elif intencion == "COMPRA_DIRECTA":
            r = await asyncio.to_thread(inventario.registrar_compra_directa, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, fuente_nombre=datos["fuente_compra"], items=datos["items"])
            salida = f"Compra registrada: {len(r['registros'])} material(es), fecha {fecha}."
        elif intencion == "AJUSTE_INVENTARIO":
            r = await asyncio.to_thread(inventario.registrar_ajuste_inventario, bodega_id=bodega_id, usuario_id=usuario_id, fecha_operacion=fecha, items=datos["items"])
            salida = f"Ajuste de inventario registrado: {len(r['registros'])} material(es), fecha {fecha}."
        elif intencion == "VENTA_DESPACHO":
            r = await asyncio.to_thread(
                inventario.registrar_venta_multiple,
                bodega_id=bodega_id, 
                usuario_id=usuario_id, 
                fecha_operacion=fecha,
                items=datos.get("items", []), 
                cliente=datos.get("cliente"),
                cliente_documento=datos.get("cliente_documento"),
                cliente_telefono=datos.get("cliente_celular"),
                cliente_direccion=datos.get("cliente_direccion"),
                cliente_conductor=datos.get("cliente_conductor"),
                cliente_conductor_id=datos.get("cliente_conductor_id"),
                cliente_placa=datos.get("cliente_placa"),
                cliente_conductor_telefono=datos.get("cliente_conductor_celular"), # <--- Asegúrate de usar esta clave aquí
            )

            pdf_path = None
            numero_orden = r.get("numero_remision", "SIN-NUMERO")
            try:
                nombre_pdf = f"orden_salida_{usuario_id}_{int(datetime.now(BOGOTA).timestamp())}.pdf"
                pdf_path = os.path.join(tempfile.gettempdir(), nombre_pdf)
                conductor_reg = r.get("conductor") or {}
                await asyncio.to_thread(
                    generar_remision_pdf_archivo,
                    pdf_path,
                    fecha=fecha,
                    cliente=datos.get("cliente", "") or (r.get("cliente") or {}).get("nombre", ""),
                    documento=datos.get("cliente_documento") or (r.get("cliente") or {}).get("identificacion"),
                    direccion=datos.get("cliente_direccion") or (r.get("cliente") or {}).get("direccion"),
                    celular=datos.get("cliente_celular") or (r.get("cliente") or {}).get("telefono"),
                    placa=datos.get("cliente_placa") or conductor_reg.get("placa"),
                    conductor=datos.get("cliente_conductor") or conductor_reg.get("nombre"),
                    id_conductor=datos.get("cliente_conductor_id") or conductor_reg.get("identificacion"),
                    celular_conductor=datos.get("cliente_conductor_telefono") or conductor_reg.get("telefono"),
                    items=datos.get("items", []),
                    numero_remision=numero_orden,
                    bodega_id=bodega_id,
                    # Flujo "Orden de Salida -> Remisión Aprobada": la venta nace
                    # sin precios; el PDF es una Orden de Salida SIN valores.
                    estado=r.get("estado") or "ORDEN_SALIDA",
                )
                logger.info(f"📄 Orden de Salida generada: {pdf_path}")
                salida = (
                    f"✅ Orden de Salida #{numero_orden} registrada exitosamente: "
                    f"{len(r['registros'])} material(es).\n"
                    f"📅 Fecha: {fecha}\n"
                    f"⏳ La Orden de Salida queda pendiente de valoración y aprobación "
                    f"por el área de Contabilidad."
                )

                try:
                    await enviar_documento_whatsapp(
                        destino=telefono,
                        ruta_archivo=pdf_path,
                        nombre_documento=f"Orden_de_Salida_{numero_orden}.pdf"
                    )
                except Exception as e:
                    logger.error(f"❌ Error enviando PDF por WhatsApp: {e}")
                    salida += "\n⚠️ Orden de Salida generada pero no se pudo enviar por WhatsApp"

            except Exception as e:
                logger.error(f"❌ Error generando Orden de Salida: {e}")
                salida = f"✅ Venta registrada: {len(r['registros'])} material(es), fecha {fecha}.\n⚠️ No se pudo generar la Orden de Salida."
        else:
            contexto["borrador_pendiente"] = datos
            await guardar_contexto(usuario_id, contexto)
            
            menu_principal = [
                ("1", "Ingresar Inventario"),
                ("2", "Ver Inventario"),
                ("3", "Anular/Corregir Rem")
            ]
            
            await enviar_botones_whatsapp(
                destino=telefono,
                texto=datos.get("respuesta_texto") or "¿Qué operación deseas registrar?",
                opciones=menu_principal
            )
            return MANEJADO
        contexto["borrador_pendiente"] = {}
        contexto["campo_esperado"] = None
        await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, salida)
    except Exception as exc:
        # ERROR de validación/registro (ej. material no encontrado): se PURGAN
        # los materiales del borrador inmediatamente; si el usuario reenvía la
        # lista corregida, no se concatenan con los del intento fallido.
        logger.error(f"❌ Error registrando la operación: {exc}")
        if isinstance(contexto.get("borrador_pendiente"), dict):
            contexto["borrador_pendiente"]["items"] = []
            await guardar_contexto(usuario_id, contexto)
        await enviar_mensaje_whatsapp(telefono, f"No registré la operación: {exc}")
