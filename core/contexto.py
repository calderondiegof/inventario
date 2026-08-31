"""Persistencia del contexto de usuario y deduplicacion de mensajes de WhatsApp."""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict

from core.config import BOGOTA, supabase

logger = logging.getLogger(__name__)

# Deduplicación de mensajes de WhatsApp con TTL. Meta puede reintentar/
# reentregar un mismo webhook (timeout de DeepSeek/DB, red lenta); esto evita
# que el bot responda (y registre operaciones) varias veces por un mensaje.
# Es un DICT id -> timestamp: cada entrada EXPIRA sola (antes era un set que
# se vaciaba completo al llegar al máximo, dejando pasar reintentos viejos).
_mensajes_whatsapp_procesados: Dict[str, float] = {}
_MAX_MENSAJES_PROCESADOS = 5000
_MENSAJE_TTL_SEGUNDOS = 600.0  # Meta reintenta en minutos; 10 min cubre de sobra


def _mensaje_es_duplicado(mensaje_id: str) -> bool:
    """True si `mensaje_id` ya fue procesado dentro de la ventana TTL.
    Purga primero las entradas expiradas (purga incremental, no reset total)."""
    ahora = time.time()
    expirados = [k for k, ts in _mensajes_whatsapp_procesados.items()
                 if ahora - ts > _MENSAJE_TTL_SEGUNDOS]
    for k in expirados:
        _mensajes_whatsapp_procesados.pop(k, None)
    if len(_mensajes_whatsapp_procesados) > _MAX_MENSAJES_PROCESADOS:
        _mensajes_whatsapp_procesados.clear()
    if mensaje_id in _mensajes_whatsapp_procesados:
        return True
    _mensajes_whatsapp_procesados[mensaje_id] = ahora
    return False
def fecha_local_mensaje(message: Dict[str, Any]) -> str:
    marca = message.get("timestamp")
    if marca:
        return datetime.fromtimestamp(int(marca), tz=BOGOTA).date().isoformat()
    return datetime.now(BOGOTA).date().isoformat()
async def guardar_contexto(usuario_id: int, contexto: Dict[str, Any]) -> None:
    await asyncio.to_thread(lambda: supabase.table("usuarios").update({"contexto_operacion": contexto}).eq("id", usuario_id).execute())

