"""Capa de envio hacia la API Cloud de WhatsApp (payloads, botones, listas, archivos)."""
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from core import config as _config
from services.inventario_service import (
    construir_lista_texto_whatsapp, construir_seccion_lista_interactiva,
)

logger = logging.getLogger(__name__)


def clean_payload(obj: Any) -> Any:
    """Elimina explícitamente valores None recursivamente para evitar rechazos en la API de WhatsApp."""
    if isinstance(obj, dict):
        return {k: clean_payload(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, list):
        return [clean_payload(v) for v in obj if v is not None]
    return obj


def _telefono_limpio(destino: str) -> str:
    """Deja solo los dígitos del número, como espera la API de WhatsApp."""
    return re.sub(r"\D", "", str(destino))


def _payload_base_whatsapp(destino: str, tipo: str) -> dict:
    """Base común de todos los payloads de la API de WhatsApp."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _telefono_limpio(destino),
        "type": tipo,
    }


async def enviar_mensaje_whatsapp_json(payload: dict) -> None:
    """
    Función centralizada única para enviar JSON a la API de WhatsApp.
    Mantiene la URL oficial protegida con try-except para evitar caídas
    y limpia de forma estricta las variables para evitar errores de Render.
    """
    if not _config.http_client:
        logger.error("❌ _config.http_client no inicializado")
        return

    # Limpieza absoluta de variables para evitar textos corruptos en producción
    phone_id = str(_config.PHONE_NUMBER_ID).strip().replace('"', '').replace("'", "")
    token_limpio = str(_config.WHATSAPP_TOKEN).strip().replace('"', '').replace("'", "")
    
    # URL oficial y segura (Evita problemas de certificados SSL)
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {token_limpio}",
        "Content-Type": "application/json"
    }
    
    cleaned = clean_payload(payload)
    destino = cleaned.get("to", "Desconocido")
    
    logger.info(f"📤 Enviando payload seguro a {destino}")
    
    try:
        # Petición controlada usando el cliente global
        response = await _config.http_client.post(url, json=cleaned, headers=headers)
        
        if response.status_code == 401:
            logger.error("❌ Error 401: El token de administrador de Meta no es válido o expiró.")
            return
            
        response.raise_for_status()
        logger.info(f"✅ Respuesta WhatsApp API: {response.status_code}")
        
    except httpx.ReadTimeout:
        logger.error(f"⏳ Tiempo de espera agotado (Timeout) con Meta API para el destino: {destino}")
    except httpx.ReadError as exc:
        logger.error(f"📡 Error de red temporal en Render (Evitando caída del servidor): {exc}")
    except httpx.HTTPStatusError as exc:
        logger.error(f"💥 Meta API devolvió un error de estado {exc.response.status_code}: {exc.response.text}")
    except Exception as e:
        logger.error(f"⚠️ Error inesperado controlado en el envío de WhatsApp: {e}")

async def enviar_mensaje_whatsapp(destino: str, texto: str) -> None:
    payload = _payload_base_whatsapp(destino, "text")
    payload["text"] = {"body": str(texto)[:4096]}
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_botones_whatsapp(destino: str, texto: str, opciones: List[tuple]) -> None:
    botones = [{"type": "reply", "reply": {"id": id_, "title": titulo[:20]}} for id_, titulo in opciones[:3]]
    payload = _payload_base_whatsapp(destino, "interactive")
    payload["interactive"] = {
        "type": "button",
        "body": {"text": texto},
        "action": {"buttons": botones},
    }
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_lista_whatsapp(destino: str, texto: str, titulo_boton: str,
                                filas: List[tuple], titulo_lista: str = "Opciones") -> None:
    """Envía un catálogo/listado de materiales por WhatsApp con el método que
    respete el límite de la API.

    - Si hay <=10 elementos: Interactive List Message (máx 10 filas totales;
      superarlo dispara el error 400 #131009 de Meta). Cada fila es
      (id, titulo, [descripcion]); al elegir un ítem, el webhook entrega su `id`
      en interactive.list_reply.id.
    - Si hay >10 elementos (ej. 30+ materiales): NO se usa el tipo 'list'; se
      envía un mensaje de TEXTO normal con viñetas ordenado alfabéticamente
      (construir_lista_texto_whatsapp). El nombre del material (title) queda
      disponible para que el usuario lo escriba y reenganche el flujo."""
    filas = list(filas)
    if len(filas) > 10:
        nombres = [str(f[1]) for f in filas]
        await enviar_mensaje_whatsapp(destino,
                                      construir_lista_texto_whatsapp(nombres, titulo=titulo_lista))
        return
    sections = construir_seccion_lista_interactiva(filas, titulo_lista=titulo_lista)
    payload = _payload_base_whatsapp(destino, "interactive")
    payload["interactive"] = {
        "type": "list",
        "body": {"text": texto},
        "action": {
            "button": titulo_boton[:20],
            "sections": sections,
        },
    }
    await enviar_mensaje_whatsapp_json(payload)


async def enviar_imagen_whatsapp(destino: str, url_imagen: str, leyenda: str) -> None:
    payload = _payload_base_whatsapp(destino, "image")
    payload["image"] = {"link": url_imagen, "caption": leyenda}
    await enviar_mensaje_whatsapp_json(payload)

async def enviar_documento_whatsapp(destino: str, ruta_archivo: str, nombre_documento: str = "documento.pdf") -> None:
    if not _config.http_client:
        logger.error("❌ _config.http_client no inicializado")
        return

    logger.info(f"📄 Enviando documento a {destino}: {ruta_archivo}")

    try:
        if not os.path.exists(ruta_archivo):
            logger.error(f"❌ Archivo no encontrado: {ruta_archivo}")
            return

        try:
            url_documento = await subir_archivo_supabase(ruta_archivo, nombre_documento)
        except Exception as e:
            logger.warning(f"⚠️ No se pudo subir a Supabase: {e}, intentando con servidor local")
            base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
            url_documento = f"{base_url}/download/{os.path.basename(ruta_archivo)}"

        payload = _payload_base_whatsapp(destino, "document")
        payload["document"] = {
            "link": url_documento,
            "filename": nombre_documento,
        }
        await enviar_mensaje_whatsapp_json(payload)

    except Exception as e:
        logger.error(f"❌ Error enviando documento: {e}")


async def subir_archivo_supabase(ruta_archivo: str, nombre_documento: str) -> str:
    if not _config.supabase or not _config.http_client:
        raise Exception("Supabase no configurado")

    try:
        with open(ruta_archivo, 'rb') as f:
            contenido = f.read()

        timestamp = int(datetime.now(_config.BOGOTA).timestamp())
        ruta_storage = f"remisiones/{timestamp}_{nombre_documento}"

        _config.supabase.storage.from_("documentos").upload(
            ruta_storage,
            contenido,
            {"content-type": "application/pdf"}
        )

        logger.info(f"✅ Archivo subido a Supabase: {ruta_storage}")

        url_publica = _config.supabase.storage.from_("documentos").get_public_url(ruta_storage)
        return url_publica

    except Exception as e:
        logger.error(f"❌ Error subiendo a Supabase: {e}")
        raise
