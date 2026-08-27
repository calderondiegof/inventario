"""Pruebas offline del servicio de tasa del dolar (TRM / Dolar Blue).

Ejecutar sin red real:
    python -m tests.test_currency_service

La capa de red (`_get_json`) se mockea por completo: no hay llamadas HTTP.
Se validan el enrutamiento Argentina/otros paises, el calculo de la tasa
intermedia (compra+venta)/2, el fallback a None ante fallos y el parsing.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# El directorio local `supabase/` (migraciones SQL) ENSOMBRECE al paquete pip
# `supabase` cuando el CWD es la raiz del proyecto. `services/__init__.py`
# importa inventario_service, que usa `Client` solo como anotacion de tipo.
_stub = types.ModuleType("supabase")
_stub.Client = object
sys.modules.setdefault("supabase", _stub)

import httpx  # noqa: E402

import services.currency_service as cs  # noqa: E402
from services.currency_service import obtener_tasa_dolar  # noqa: E402


_FAILURES = []


def _ok(nombre, cond):
    print(("PASS " if cond else "FAIL ") + nombre)
    if not cond:
        _FAILURES.append(nombre)


def _async(coro_fn):
    """Ejecuta una corutina sincronicamente dentro del test."""
    return asyncio.run(coro_fn())


# ---------------------------------------------------------------------------
# Utilidades de mock: reemplazan cs._get_json por fakes deterministas
# ---------------------------------------------------------------------------
class _Ctx:
    """Registra la ultima URL consultada y el comportamiento del fake."""

    def __init__(self):
        self.url = None
        self.data = None
        self.exc = None


def _patch_get_json(ctx):
    async def fake_get_json(url):
        ctx.url = url
        if ctx.exc is not None:
            raise ctx.exc
        return ctx.data

    cs._get_json = fake_get_json


_DATA_BLUE = {"compra": 1000.0, "venta": 1040.0, "fechaActualizacion": "2026-08-27"}
_DATA_RATES = {"result": "success", "base_code": "USD",
               "rates": {"COP": 4100.5, "PEN": 3.75, "MXN": 18.2, "CLP": 950.0}}


# ---------------------------------------------------------------------------
# Tests: Argentina -> Dolar Blue (tasa intermedia)
# ---------------------------------------------------------------------------
def test_argentina_promedio():
    ctx = _Ctx()
    ctx.data = dict(_DATA_BLUE)
    _patch_get_json(ctx)
    tasa = _async(lambda: obtener_tasa_dolar(pais="Argentina", moneda="ARS"))
    _ok("Argentina: tasa intermedia (1000+1040)/2 = 1020", tasa == 1020.0)
    _ok("Argentina: consulta la URL de DolarApi Blue", ctx.url == cs.DOLAR_BLUE_URL)


def test_argentina_case_insensitive():
    ctx = _Ctx()
    ctx.data = dict(_DATA_BLUE)
    _patch_get_json(ctx)
    tasa1 = _async(lambda: obtener_tasa_dolar(pais="ARGENTINA", moneda=""))
    tasa2 = _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="ARS"))
    _ok("Argentina: pais en mayusculas -> Blue", tasa1 == 1020.0)
    _ok("Argentina: moneda 'ARS' gana aunque el pais sea otro", tasa2 == 1020.0)


def test_argentina_compra_venta_strings():
    """La API puede devolver numeros como texto: debe tolerarlo."""
    ctx = _Ctx()
    ctx.data = {"compra": "1010.5", "venta": "1039.5"}
    _patch_get_json(ctx)
    tasa = _async(lambda: obtener_tasa_dolar(pais="argentina", moneda="ARS"))
    _ok("Argentina: acepta compra/venta como strings", tasa == 1025.0)


# ---------------------------------------------------------------------------
# Tests: otros paises -> TRM / rates oficiales
# ---------------------------------------------------------------------------
def test_colombia_trm():
    ctx = _Ctx()
    ctx.data = dict(_DATA_RATES)
    _patch_get_json(ctx)
    tasa = _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="COP"))
    _ok("Colombia: TRM rates[COP] = 4100.5", tasa == 4100.5)
    _ok("Colombia: consulta la URL de er-api", ctx.url == cs.ER_RATES_URL)


def test_moneda_inferida_del_pais():
    ctx = _Ctx()
    ctx.data = dict(_DATA_RATES)
    _patch_get_json(ctx)
    pen = _async(lambda: obtener_tasa_dolar(pais="Peru", moneda=""))
    mxn = _async(lambda: obtener_tasa_dolar(pais="México", moneda="MXN"))
    _ok("Moneda: inferida del mapa si no se pasa (Peru -> PEN)", pen == 3.75)
    _ok("Moneda: explicita gana sobre el mapa (Mexico + MXN)", mxn == 18.2)


def test_moneda_default_usd_pais_desconocido():
    ctx = _Ctx()
    ctx.data = {"rates": {"USD": 1.0}}
    _patch_get_json(ctx)
    tasa = _async(lambda: obtener_tasa_dolar(pais="PaisInexistente", moneda=""))
    _ok("Moneda: pais desconocido cae a USD por defecto", tasa == 1.0)



# ---------------------------------------------------------------------------
# Tests: fallback ante fallos (nunca debe lanzar excepciones)
# ---------------------------------------------------------------------------
def test_fallo_red_argentina():
    ctx = _Ctx()
    ctx.exc = httpx.ConnectError("conexion rechazada")
    _patch_get_json(ctx)
    tasa = _async(lambda: obtener_tasa_dolar(pais="Argentina", moneda="ARS"))
    _ok("Fallback: ConnectError en Blue -> None sin lanzar", tasa is None)


def test_fallo_red_otros():
    ctx = _Ctx()
    ctx.exc = httpx.TimeoutException("timeout 12s")
    _patch_get_json(ctx)
    tasa = _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="COP"))
    _ok("Fallback: Timeout en er-api -> None sin lanzar", tasa is None)


def test_fallo_http_status():
    ctx = _Ctx()
    req = httpx.Request("GET", cs.ER_RATES_URL)
    ctx.exc = httpx.HTTPStatusError("503 Service Unavailable",
                                    request=req, response=httpx.Response(503))
    _patch_get_json(ctx)
    tasa = _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="COP"))
    _ok("Fallback: HTTP 503 -> None sin lanzar", tasa is None)


def test_json_invalido():
    ctx = _Ctx()
    ctx.exc = ValueError("json invalido")
    _patch_get_json(ctx)
    _ok("Fallback: JSON invalido -> None sin lanzar",
        _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="COP")) is None)


# ---------------------------------------------------------------------------
# Tests: datos presentes pero incompletos / invalidos
# ---------------------------------------------------------------------------
def test_datos_incompletos_blue():
    ctx = _Ctx()
    _patch_get_json(ctx)

    ctx.data = {"compra": 1000.0}          # falta "venta"
    t1 = _async(lambda: obtener_tasa_dolar(pais="Argentina", moneda="ARS"))
    ctx.data = {"venta": 1040.0}           # falta "compra"
    t2 = _async(lambda: obtener_tasa_dolar(pais="Argentina", moneda="ARS"))
    ctx.data = {"compra": None, "venta": "abc"}  # valores no numericos
    t3 = _async(lambda: obtener_tasa_dolar(pais="Argentina", moneda="ARS"))
    _ok("Blue: sin 'venta' -> None", t1 is None)
    _ok("Blue: sin 'compra' -> None", t2 is None)
    _ok("Blue: valores no numericos -> None", t3 is None)


def test_datos_incompletos_rates():
    ctx = _Ctx()
    _patch_get_json(ctx)

    ctx.data = {"result": "success"}       # sin clave "rates"
    t1 = _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="COP"))
    ctx.data = {"rates": {"MXN": 18.2}}    # rates sin COP
    t2 = _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="COP"))
    ctx.data = {"rates": {"COP": "no-es-numero"}}  # valor no convertible
    t3 = _async(lambda: obtener_tasa_dolar(pais="Colombia", moneda="COP"))
    _ok("Rates: respuesta sin 'rates' -> None", t1 is None)
    _ok("Rates: 'rates' sin la moneda pedida -> None", t2 is None)
    _ok("Rates: valor no numerico en rates -> None", t3 is None)


# ---------------------------------------------------------------------------
# Tests: helpers puros (sin red)
# ---------------------------------------------------------------------------
def test_helpers_puros():
    _ok("Helper: _es_argentina('Argentina','')", cs._es_argentina("Argentina", ""))
    _ok("Helper: _es_argentina('X','ars') case-insensitive", cs._es_argentina("X", "ars"))
    _ok("Helper: _es_argentina('Colombia','COP') es False", not cs._es_argentina("Colombia", "COP"))
    _ok("Helper: _normalizar_moneda explicita gana", cs._normalizar_moneda("Peru", "pen") == "PEN")
    _ok("Helper: _normalizar_moneda inferida por pais", cs._normalizar_moneda("Chile", "") == "CLP")
    _ok("Helper: _normalizar_moneda default USD", cs._normalizar_moneda("Narnia", "") == "USD")
    _ok("Helper: _normalizar_moneda inputs vacios -> USD", cs._normalizar_moneda("", "") == "USD")
    _ok("Helper: promedio None si falta compra", cs._promedio_compra_venta({"venta": 1}) is None)
    _ok("Helper: tasa None si rates vacio", cs._tasa_de_rates({}, "COP") is None)


# ---------------------------------------------------------------------------
def main():
    test_argentina_promedio()
    test_argentina_case_insensitive()
    test_argentina_compra_venta_strings()
    test_colombia_trm()
    test_moneda_inferida_del_pais()
    test_moneda_default_usd_pais_desconocido()
    test_fallo_red_argentina()
    test_fallo_red_otros()
    test_fallo_http_status()
    test_json_invalido()
    test_datos_incompletos_blue()
    test_datos_incompletos_rates()
    test_helpers_puros()
    if _FAILURES:
        print("\n=== %d FALLO(S) ===\n%s" % (len(_FAILURES), "\n".join(" - " + f for f in _FAILURES)))
        return 1
    print("\nTODAS LAS PRUEBAS PASARON OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
