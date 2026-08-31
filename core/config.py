"""Configuracion central: variables de entorno, cliente Supabase y constantes.

`http_client` se inicializa en el lifespan de la app (main.py) y SIEMPRE
se accede como atributo (`config.http_client`)."""
import logging
import os
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from supabase import Client, create_client

from services.inventario_service import InventarioServiceConValidacion

# Configuración de Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cargar variables de entorno
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()

logger.warning("=" * 60)
logger.warning("VERIFICACIÓN DE VARIABLES DE CONFIGURACIÓN:")
logger.warning(f"✓ SUPABASE_URL: {'✅' if SUPABASE_URL else '❌'}")
logger.warning(f"✓ SUPABASE_KEY: {'✅' if SUPABASE_KEY else '❌'}")
logger.warning(f"✓ DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
logger.warning(f"✓ VERIFY_TOKEN: {'✅' if VERIFY_TOKEN else '❌'}")
logger.warning(f"✓ WHATSAPP_TOKEN: {'✅' if WHATSAPP_TOKEN else '❌'}")
logger.warning(f"✓ PHONE_NUMBER_ID: {'✅' if PHONE_NUMBER_ID else '❌'}")
logger.warning(f"✓ META_APP_SECRET: {'✅' if META_APP_SECRET else '❌'}")
logger.warning("=" * 60)

# Inicialización de Supabase
supabase: Optional[Client] = None
inventario: Optional[InventarioServiceConValidacion] = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        inventario = InventarioServiceConValidacion(supabase)
        logger.info("✅ Supabase conectado")
    except Exception as e:
        logger.error(f"❌ Error conectando a Supabase: {e}")
else:
    logger.warning("⚠️ Supabase no configurado")

http_client: Optional[httpx.AsyncClient] = None
BOGOTA = ZoneInfo("America/Bogota")

