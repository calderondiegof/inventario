"""Punto de entrada de la app FastAPI: webhooks de WhatsApp y delegacion al router de handlers."""
import hashlib
import hmac
import json
import logging
import os
import tempfile
import uvicorn
from contextlib import asynccontextmanager
from typing import Any, Dict

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response

from core import config
from core.config import (
    DEEPSEEK_API_KEY, META_APP_SECRET, PHONE_NUMBER_ID, SUPABASE_KEY,
    SUPABASE_URL, VERIFY_TOKEN, WHATSAPP_TOKEN, BOGOTA, inventario, supabase,
)
from core.contexto import _mensaje_es_duplicado
from core.whatsapp import enviar_mensaje_whatsapp
from handlers.router import procesar_un_mensaje

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.http_client = httpx.AsyncClient(timeout=httpx.Timeout(35.0))
    yield
    await config.http_client.aclose()


app = FastAPI(title="Agente de Inventario", lifespan=lifespan)



async def procesar_webhook(data: Dict[str, Any]) -> None:
    logger.info(f"📥 Procesando webhook: {json.dumps(data, ensure_ascii=False)[:500]}")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                mensaje_id = message.get("id")
                if mensaje_id:
                    if _mensaje_es_duplicado(mensaje_id):
                        logger.info(f"⏭️ Mensaje {mensaje_id} ya procesado (o dentro de la ventana TTL); se omite para evitar duplicados.")
                        continue
                logger.info(f"📨 Mensaje nuevo: {message}")
                try:
                    resultado = await procesar_un_mensaje(message, value.get("contacts", []))
                    # Safety net: si el router devolvió un texto (fallback que
                    # no se envió por sí mismo), se envía aquí para que el bot
                    # nunca quede mudo ante un flujo no gestionado internamente.
                    if isinstance(resultado, str) and resultado:
                        telefono = str(message.get("from", "")).replace("+", "")
                        await enviar_mensaje_whatsapp(telefono, resultado)
                except Exception:
                    # NUNCA silenciar: una excepción aquí (error de PostgREST,
                    # red, bug) deja al bot mudo sin que el operador lo note.
                    logger.exception("❌ Error no controlado procesando el mensaje de WhatsApp")


# Endpoints HTTP / Webhook API
@app.get("/")
def raiz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/test")
def test() -> Dict[str, Any]:
    return {
        "status": "ok",
        "VERIFY_TOKEN_set": "✅" if VERIFY_TOKEN else "❌",
        "WHATSAPP_TOKEN_set": "✅" if WHATSAPP_TOKEN else "❌",
        "PHONE_NUMBER_ID_set": "✅" if PHONE_NUMBER_ID else "❌",
        "META_APP_SECRET_set": "✅" if META_APP_SECRET else "❌",
        "DEEPSEEK_API_KEY_set": "✅" if DEEPSEEK_API_KEY else "❌",
    }


@app.get("/debug")
def debug() -> Dict[str, Any]:
    logger.info("🔍 Endpoint debug llamado")
    return {
        "status": "Servidor funcionando",
        "VERIFY_TOKEN_value": VERIFY_TOKEN[:5] + "..." if VERIFY_TOKEN else "❌ NO CONFIGURADO",
        "PHONE_NUMBER_ID_value": PHONE_NUMBER_ID if PHONE_NUMBER_ID else "❌ NO CONFIGURADO",
        "META_APP_SECRET_configured": bool(META_APP_SECRET),
        "WHATSAPP_TOKEN_configured": bool(WHATSAPP_TOKEN),
    }


@app.get("/webhook")
async def verificar_webhook(request: Request) -> Response:
    p = request.query_params
    logger.info(f"GET /webhook - Parámetros: {dict(p)}")
    logger.info(f"VERIFY_TOKEN configurado: {bool(VERIFY_TOKEN)}")
    logger.info(f"Token recibido: {p.get('hub.verify_token', '(vacío)')}")
    if p.get("hub.mode") == "subscribe" and hmac.compare_digest(p.get("hub.verify_token", ""), VERIFY_TOKEN):
        logger.info("✅ Webhook verificado correctamente")
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    logger.warning("❌ Validación fallida - Token inválido o modo incorrecto")
    return Response("Token inválido", status_code=403)

@app.post("/webhook")
async def webhook_whatsapp(request: Request, background_tasks: BackgroundTasks) -> Response:
    cuerpo = await request.body()
    firma = request.headers.get("X-Hub-Signature-256", "")
    logger.info(f"📨 POST /webhook - Recibido webhook")
    logger.info(f"Firma recibida: {firma[:30] if firma else '(vacía)'}...")
    logger.info(f"Cuerpo: {cuerpo[:300]}...")
    if not META_APP_SECRET:
        # Sin app secret configurado NO se procesa ningún evento: si se aceptara,
        # cualquier POST a esta URL pública podría hacer que el bot enviara
        # mensajes de WhatsApp por sí solo. Es más seguro rechazarlo.
        logger.warning("❌ META_APP_SECRET no configurado: se rechaza el webhook (no se procesa).")
        logger.warning("   El bot NO enviará mensajes de WhatsApp sin firma válida de Meta.")
        return Response("Firma no configurada", status_code=403)
    esperada = "sha256=" + hmac.new(META_APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
    logger.info(f"Validando firma - Esperada: {esperada[:30]}...")
    if not hmac.compare_digest(firma, esperada):
        logger.warning("❌ Firma inválida: se rechaza el evento y NO se envía ningún mensaje.")
        return Response("Firma inválida", status_code=403)
    logger.info("✅ Firma validada")

    try:
        datos = json.loads(cuerpo)
        logger.info(f"✅ JSON parseado correctamente")
        background_tasks.add_task(procesar_webhook, datos)
    except json.JSONDecodeError as e:
        logger.error(f"❌ Error parseando JSON: {e}")
        return Response(f"Error parseando JSON: {e}", status_code=400)

    return Response("EVENT_RECEIVED", status_code=200)


@app.get("/download/{nombre_archivo}")
async def descargar_documento(nombre_archivo: str) -> Response:
    ruta_archivo = os.path.join(tempfile.gettempdir(), nombre_archivo)

    if not os.path.exists(ruta_archivo):
        logger.warning(f"⚠️ Archivo no encontrado: {ruta_archivo}")
        return Response("Archivo no encontrado", status_code=404)

    try:
        with open(ruta_archivo, 'rb') as f:
            contenido = f.read()

        return Response(
            content=contenido,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{nombre_archivo}"'
            }
        )
    except Exception as e:
        logger.error(f"❌ Error descargando archivo: {e}")
        return Response("Error descargando archivo", status_code=500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

