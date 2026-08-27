"""Servicio externo para consultar la tasa representativa del dólar (TRM / Dólar Blue).

Uso (asíncrono, ejecutar desde un loop de eventos como el de FastAPI):

    tasa_col = await obtener_tasa_dolar(pais="Colombia", moneda="COP")
    tasa_ars = await obtener_tasa_dolar(pais="Argentina", moneda="ARS")

Reglas de negocio:
  - Argentina / moneda "ARS" -> Dólar Blue vía DolarApi
    (``https://dolarapi.com/v1/dolares/blue``). Se devuelve la tasa intermedia
    ``(compra + venta) / 2``.
  - Otros países / monedas      -> TRM oficial (unidad bilateral USD -> moneda
    local, ej. USD/COP) vía la API pública y sin clave ``open.er-api.com``
    (``https://open.er-api.com/v6/latest/USD``, campo ``rates[<CODIGO_ISO>]``).

Manejo de errores / fallback:
  - Ante fallo de red, timeout, respuesta HTTP != 200, JSON mal formado o dato
    ausente, la función **no lanza excepciones**: registra el motivo en el log
    y devuelve ``None``. Así el flujo conversacional del bot no se corta
    (en ``main.py`` se decidirá qué responder si la tasa no está disponible).

La capa de red se aísla en ``_get_json()`` (inyectable/mockeable en pruebas).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Endpoints públicos (sin API key) usados por el servicio.
DOLAR_BLUE_URL = "https://dolarapi.com/v1/dolares/blue"
ER_RATES_URL = "https://open.er-api.com/v6/latest/USD"

# Tiempo máximo de espera por la respuesta HTTP.
TIMEOUT_SECONDS = 12.0

# Código ISO 4217 por defecto según el país (si no se pasa ``moneda``).
_MONEDA_POR_PAIS: Dict[str, str] = {
    "colombia": "COP",
    "ecuador": "USD",   # dolarizado: altamente USD
    "peru": "PEN",
    "mexico": "MXN",
    "chile": "CLP",
    "brasil": "BRL",
}


# ---------------------------------------------------------------------------
# Determinación de moneda / URL
# ---------------------------------------------------------------------------
def _normalizar_moneda(pais: str, moneda: str) -> str:
    """Devuelve el código ISO 4217 a consultar.

    Prioridad: el código ``moneda`` explícito > mapa por ``pais`` > USD por defecto.
    """
    m = (moneda or "").strip().upper()
    if m:
        return m
    p = (pais or "").strip().lower()
    return _MONEDA_POR_PAIS.get(p, "USD")


def _es_argentina(pais: str, moneda: str) -> bool:
    return (moneda or "").strip().upper() == "ARS" or (pais or "").strip().lower() == "argentina"


# ---------------------------------------------------------------------------
# Capa de red (aislada para poder mockearla en las pruebas)
# ---------------------------------------------------------------------------
async def _get_json(url: str) -> Dict[str, Any]:
    """HACE la petición GET (asíncrona) y devuelve el JSON de respuesta.

    Puede lanzar httpx.HTTPError / ValueError: el llamador las captura para
    entregar un fallback amigable.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=True) as client:
        response = await client.get(url)
    response.raise_for_status()  # lanza HTTPStatusError si != 2xx
    return response.json()


# ---------------------------------------------------------------------------
# Parsing por proveedor
# ---------------------------------------------------------------------------
def _promedio_compra_venta(datos: Dict[str, Any]) -> Optional[float]:
    """Tasa intermedia del Dólar Blue: (compra + venta) / 2."""
    compra = datos.get("compra")
    venta = datos.get("venta")
    if compra is None or venta is None:
        return None
    try:
        return (float(compra) + float(venta)) / 2.0
    except (TypeError, ValueError):
        return None


def _tasa_de_rates(datos: Dict[str, Any], codigo: str) -> Optional[float]:
    """Extrae ``rates[<CODIGO>]`` (unidades de moneda local por 1 USD)."""
    rates = datos.get("rates") or {}
    valor = rates.get(codigo)
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Funciones públicas
# ---------------------------------------------------------------------------
async def obtener_tasa_dolar(pais: str, moneda: str = "") -> Optional[float]:
    """Tasa representativa del dólar para el país/moneda indicado.

    Args:
        pais:   País de consulta ("Argentina", "Colombia", ...).
        moneda: Código ISO 4217 (opcional; p. ej. "ARS", "COP"). Se infiere
                del país si se omite.

    Returns:
        float con la tasa (divisas locales por 1 USD), o ``None`` si no está
        disponible (red caída, error HTTP, datos inválidos...).
    """
    # ── Argentina / ARS ──> Dólar Blue (tasa intermedia)
    if _es_argentina(pais, moneda):
        try:
            datos = await _get_json(DOLAR_BLUE_URL)
        except Exception as exc:  # httpx.HTTPError, TimeoutError, ValueError...
            logger.warning("Dólar Blue no disponible (%s): %s", DOLAR_BLUE_URL, exc)
            return None

        tasa = _promedio_compra_venta(datos)
        if tasa is None:
            logger.warning("DolarAPI no devolvió compra/venta válidas: %s", datos)
        return tasa

    # ── Resto -> TRM/divisas oficiales (USD como base)
    codigo = _normalizar_moneda(pais, moneda)
    try:
        datos = await _get_json(ER_RATES_URL)
    except Exception as exc:
        logger.warning("TRM no pudo consultarse (%s): %s", ER_RATES_URL, exc)
        return None

    tasa = _tasa_de_rates(datos, codigo)
    if tasa is None:
        logger.warning("No hay tasa de cambio para '%s' en la respuesta.", codigo)
    return tasa