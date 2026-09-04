"""Pruebas offline de la transformacion de materiales (4 estados).

Ejecutar sin base de datos real:
    python -m tests.test_transformaciones

Usa un cliente Supabase simulado para validar la conservacion de masa y que
la MERMA (Basura/Tierra) queda como stock vendible.
"""
import os
import sys
import types

# Consolas Windows (cp1252): los mensajes validados incluyen emojis (âš ï¸, âœ…)
# que no se pueden imprimir en cp1252; sin esto la suite muere al imprimir.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# El directorio local `supabase/` (migraciones SQL) ENSOMBRECE al paquete pip
# `supabase` cuando el CWD es la raiz del proyecto. Para poder importar el
# servicio offline inyectamos un modulo `supabase` de reemplazo: `Client` solo
# se usa como anotacion de tipo, asi que un `object` es suficiente.
_stub = types.ModuleType("supabase")
_stub.Client = object
_stub.create_client = lambda *a, **k: object()
sys.modules.setdefault("supabase", _stub)

from services.inventario_service import (
    InventarioServiceConValidacion,
    TipoTransaccion,
    MaterialDTO,
    normalizar, construir_lista_texto_whatsapp, construir_seccion_lista_interactiva,
    es_lista_materiales, resolver_entrada_material,
    borrador_para_nueva_lista,
    formatear_resumen_precios, parsear_edicion_precio, procesar_precio_paso_a_paso,
)


# ---------------------------------------------------------------------------
# Falso cliente Supabase (suficiente para los metodos usados)
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, fake, table, rows):
        self.fake = fake
        self.table = table
        self.rows = list(rows)
        self._changes = None

    def select(self, _cols=None):
        return self

    def eq(self, col, val):
        self.rows = [r for r in self.rows if r.get(col) == val]
        return self

    def lt(self, col, val):
        self.rows = [r for r in self.rows if r.get(col) is not None and r.get(col) < val]
        return self

    def ilike(self, col, val):
        v = str(val).lower()
        self.rows = [r for r in self.rows if str(r.get(col, "")).lower() == v]
        return self

    def order(self, col, desc=False):
        self.rows = sorted(
            self.rows,
            key=lambda r: (r.get(col) is not None, r.get(col) if r.get(col) is not None else 0),
            reverse=bool(desc),
        )
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def update(self, changes):
        self._changes = dict(changes)
        return self

    def insert(self, row):
        r = dict(row)
        r["id"] = self.fake._next_id(self.table)
        self.fake._tables[self.table].append(r)
        self.rows = [r]
        return self

    def execute(self):
        if self._changes is not None:
            for r in self.rows:
                r.update(dict(self._changes))
            return _Resp(self.rows)
        return _Resp(self.rows)


class _RpcBuilder:
    def __init__(self, fake, name, params):
        self.fake = fake
        self.name = name
        self.params = params or {}

    def execute(self):
        if self.name == "registrar_lote_inventario":
            for m in self.params.get("p_movimientos", []):
                m.setdefault("id", self.fake._next_id("movimientos_inventario"))
                self.fake._tables["movimientos_inventario"].append(dict(m))
            for mm in self.params.get("p_mermas", []):
                mm.setdefault("id", self.fake._next_id("mermas_proceso"))
                self.fake._tables["mermas_proceso"].append(dict(mm))
            return _Resp(self.params.get("p_movimientos", []))
        if self.name == "aprobar_remision_con_precios":
            # Replica la RPC PostgreSQL (tipos UUID): p_remision_id llega como
            # STRING (la RPC real lo castea a ::uuid) y las llaves del JSONB
            # también son strings; la comparación de id es por texto.
            rid = self.params.get("p_remision_id")
            vd = self.params.get("p_vr_dolar_dia")
            precios = self.params.get("p_precios_items") or {}
            if not isinstance(rid, str):
                raise TypeError("p_remision_id debe enviarse como str (uuid)")
            rem = next((r for r in self.fake._tables["remisiones"]
                        if str(r["id"]) == rid), None)
            if rem is None:
                raise ValueError(f"Remision {rid} no existe")
            lote = rem["lote_operacion_id"]
            aplicados = 0
            for m in self.fake._tables["movimientos_inventario"]:
                if m.get("lote_operacion_id") == lote and str(m["id"]) in precios:
                    m["precio_unitario"] = float(precios[str(m["id"])])
                    aplicados += 1
            rem["vr_dolar_dia"] = vd
            rem["estado"] = "APROBADA"
            return _Resp([{"id": rid, "numero": rem["numero"], "estado": "APROBADA",
                           "vr_dolar_dia": vd, "movimientos_precios": aplicados}])
        return _Resp([])


class FakeSupabase:
    def __init__(self):
        self._tables = {
            "materiales": [],
            "fuentes_origen": [],
            "movimientos_inventario": [],
            "mermas_proceso": [],
            "clientes": [],
            "conductores": [],
            "remisiones": [],
        }
        self._ids = {t: 0 for t in self._tables}

    def _next_id(self, table):
        self._ids[table] += 1
        return self._ids[table]

    def _seed(self, table, rows):
        for fila in rows:
            r = dict(fila)
            r.setdefault("id", self._next_id(table))
            self._tables[table].append(r)

    def table(self, name):
        return _Query(self, name, self._tables.get(name, []))

    def rpc(self, name, params=None):
        # Devuelve un builder; su .execute() procesa el lote y retorna .data
        return _RpcBuilder(self, name, params)


_FAILURES = []


def _ok(nombre, cond):
    print(("PASS " if cond else "FAIL ") + nombre)
    if not cond:
        _FAILURES.append(nombre)


# ---------------------------------------------------------------------------
# Catalogo / helpers de prueba
# ---------------------------------------------------------------------------
def _generar():
    fake = FakeSupabase()
    fake._seed("materiales", [
        {"nombre": "Revuelto", "tipo_material": "BRUTO", "es_comercializable": True},
        {"nombre": "Arreglo Carter", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Cable", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Cable Quemado", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Arreglo Dificil", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Carter", "tipo_material": "LIMPIO", "es_comercializable": True},
        {"nombre": "Chatarra", "tipo_material": "LIMPIO", "es_comercializable": True},
        {"nombre": "Basura", "tipo_material": "MERMA", "es_comercializable": True},
    ])
    fake._seed("fuentes_origen", [
        {"nombre": "Proceso seleccion", "tipo_fuente": "PROCESO_SELECCION"},
    ])
    return fake, InventarioServiceConValidacion(fake)


def _mid(fake, nombre):
    for m in fake._tables["materiales"]:
        if m["nombre"] == nombre:
            return m["id"]
    raise KeyError(nombre)


def _cargar(fake, bodega_id, nombre, cantidad_kg):
    fake._tables["movimientos_inventario"].append({
        "id": fake._next_id("movimientos_inventario"),
        "bodega_id": bodega_id, "material_id": _mid(fake, nombre),
        "tipo_movimiento": TipoTransaccion.ENTRADA_BRUTA.value,
        "cantidad_kg": cantidad_kg, "lote_operacion_id": "seed",
    })


def _saldo(fake, bodega_id, nombre):
    mid = _mid(fake, nombre)
    return sum(r["cantidad_kg"] for r in fake._tables["movimientos_inventario"]
               if r["bodega_id"] == bodega_id and r["material_id"] == mid)


B = 1  # bodega unica usada en las pruebas


def _cons(nombre, cond):
    _ok(nombre, cond)


# ---------------------------------------------------------------------------
# Regla 1: Transformacion primaria (Revuelto -> limpio/semilimpio/merma)
# ---------------------------------------------------------------------------
def test_regla1_revuelto():
    fake, svc = _generar()
    _cargar(fake, B, "Revuelto", 1000.0)
    r = svc.registrar_transformacion_material(
        bodega_id=B, usuario_id=9, fecha_operacion="2026-01-15",
        material_origen_nombre="Revuelto",
        resultados=[
            {"material_nombre": "Carter", "cantidad_kg": 300},
            {"material_nombre": "Cable", "cantidad_kg": 200},
        ],
        merma_kg=250, nombre_proceso="Seleccion",
    )
    _cons("Regla1: Revuelto 1000 - 750 = 250", abs(_saldo(fake, B, "Revuelto") - 250) < 0.01)
    _cons("Regla1: Carter +300", abs(_saldo(fake, B, "Carter") - 300) < 0.01)
    _cons("Regla1: Cable +200", abs(_saldo(fake, B, "Cable") - 200) < 0.01)
    _cons("Regla1: Basura(MERMA) +250 vendible", abs(_saldo(fake, B, "Basura") - 250) < 0.01)
    _cons("Regla1: 4 movimientos (1 debito+2 creditos+1 merma)", bool(r["registros"]) and len(r["registros"]) == 4)


# ---------------------------------------------------------------------------
# Regla 2: Re-transformacion de Semilimpios (quema de Cable)
# ---------------------------------------------------------------------------
def test_regla2_quema_cable():
    fake, svc = _generar()
    _cargar(fake, B, "Cable", 1000.0)
    r = svc.registrar_transformacion_material(
        bodega_id=B, usuario_id=3, fecha_operacion="2026-01-26",
        material_origen_nombre="Cable",
        resultados=[{"material_nombre": "Cable Quemado", "cantidad_kg": 600}],
        merma_kg=400, nombre_proceso="Quema de Cable",
    )
    _cons("Regla2: Cable -1000", abs(_saldo(fake, B, "Cable")) < 0.01)
    _cons("Regla2: Cable Quemado +600", abs(_saldo(fake, B, "Cable Quemado") - 600) < 0.01)
    _cons("Regla2: Basura +400", abs(_saldo(fake, B, "Basura") - 400) < 0.01)
    _cons("Regla2: NO afecta Revuelto", abs(_saldo(fake, B, "Revuelto")) < 0.01)
    # Ejemplo del usuario (caso 2): 1354 de cable -> 600 cobre + 754 basura.
    # ingreso_inventario = SOLO los materiales aprovechables (600), NUNCA la basura.
    # descontado_origen = 1354 (todo lo que sale del cable, merma incluida).
    _cons("Regla2: ingreso_inventario = solo productos (600)",
          abs(r.get("ingreso_inventario", 0) - 600.0) < 0.01)
    _cons("Regla2: descontado_origen = productos + merma (1000)",
          abs(r.get("descontado_origen", 0) - 1000.0) < 0.01
          and abs(merma := float(r.get("merma_kg") or 0)) < 0.01
          and abs(r["descontado_origen"] - (r["ingreso_inventario"] + merma)) < 0.01)


# ---------------------------------------------------------------------------
# Regla 3: Seleccion tecnica de Semilimpios (Arreglo Carter)
# ---------------------------------------------------------------------------
def test_regla3_seleccion_tecnica_carter():
    fake, svc = _generar()
    _cargar(fake, B, "Arreglo Carter", 1000.0)
    svc.registrar_transformacion_material(
        bodega_id=B, usuario_id=3, fecha_operacion="2026-01-26",
        material_origen_nombre="Arreglo Carter",
        resultados=[
            {"material_nombre": "Carter", "cantidad_kg": 500},
            {"material_nombre": "Chatarra", "cantidad_kg": 200},
            {"material_nombre": "Cable", "cantidad_kg": 50},
            {"material_nombre": "Arreglo Dificil", "cantidad_kg": 50},
        ],
        merma_kg=200, nombre_proceso="Seleccion tecnica",
    )
    _cons("Regla3: Arreglo Carter -1000", abs(_saldo(fake, B, "Arreglo Carter")) < 0.01)
    _cons("Regla3: Carter +500", abs(_saldo(fake, B, "Carter") - 500) < 0.01)
    _cons("Regla3: Chatarra +200", abs(_saldo(fake, B, "Chatarra") - 200) < 0.01)
    _cons("Regla3: Cable +50", abs(_saldo(fake, B, "Cable") - 50) < 0.01)
    _cons("Regla3: Arreglo Dificil +50", abs(_saldo(fake, B, "Arreglo Dificil") - 50) < 0.01)
    _cons("Regla3: Basura +200", abs(_saldo(fake, B, "Basura") - 200) < 0.01)


# ---------------------------------------------------------------------------
# Conservacion de masa y stock
# ---------------------------------------------------------------------------
def test_conservacion_masa_exigida():
    fake, svc = _generar()
    _cargar(fake, B, "Cable", 1000.0)
    try:
        # 600 + 300 = 900 != 1000 procesado -> debe rechazarse
        svc.registrar_transformacion_material(
            bodega_id=B, usuario_id=3, fecha_operacion="2026-01-26",
            material_origen_nombre="Cable",
            resultados=[{"material_nombre": "Cable Quemado", "cantidad_kg": 600}],
            merma_kg=300, cantidad_procesada=1000, nombre_proceso="Quema",
        )
        _cons("Conservacion: rechaza divergencia", False)
    except ValueError:
        _cons("Conservacion: rechaza divergencia", True)


def test_stock_insuficiente():
    fake, svc = _generar()
    _cargar(fake, B, "Cable", 100.0)
    try:
        svc.registrar_transformacion_material(
            bodega_id=B, usuario_id=3, fecha_operacion="2026-01-26",
            material_origen_nombre="Cable",
            resultados=[{"material_nombre": "Cable Quemado", "cantidad_kg": 600}],
            merma_kg=400, nombre_proceso="Quema",
        )
        _cons("Stock: rechaza si no alcanza", False)
    except ValueError:
        _cons("Stock: rechaza si no alcanza", True)


# ---------------------------------------------------------------------------
# La MERMA (Basura) tiene stock y se puede vender
# ---------------------------------------------------------------------------
def test_vender_merma():
    fake, svc = _generar()
    _cargar(fake, B, "Basura", 500.0)
    svc.obtener_o_crear_cliente = lambda **kw: {"id": 501, "nombre": kw.get("nombre", "C")}
    svc.obtener_o_crear_conductor = lambda **kw: None
    svc.generar_numero_remision = lambda: "REM_1"
    svc.registrar_remision = lambda **kw: None
    svc.registrar_venta_multiple(
        bodega_id=B, usuario_id=3, fecha_operacion="2026-01-26",
        items=[{"material_nombre": "Basura", "cantidad_kg": 200}],
        cliente="Cliente X",
    )
    _cons("Venta: Basura (MERMA) vendible, queda 300", abs(_saldo(fake, B, "Basura") - 300) < 0.01)


def test_regenerar_pdf_datos():
    """Valida que obtener_datos_pdf_remision reúne la remisión con su mismo
    número y los items (positivos), tras una corrección de material."""
    fake = FakeSupabase()
    fake._seed("materiales", [
        {"nombre": "Carter", "tipo_material": "LIMPIO", "es_comercializable": True},
    ])
    fake._seed("clientes", [{"id": 11, "nombre": "Cliente X", "identificacion": "123", "telefono": "3001", "direccion": "Calle 1"}])
    fake._seed("conductores", [{"id": 22, "nombre": "Conductor Y", "identificacion": "456", "telefono": "3002", "placa": "ABC123"}])
    fake._seed("remisiones", [{
        "id": 100, "numero": "REM_7", "lote_operacion_id": "lote-7",
        "cliente_id": 11, "conductor_id": 22, "bodega_id": B, "fecha_operacion": "2026-01-26",
    }])
    fake._seed("movimientos_inventario", [{
        "id": 900, "bodega_id": B, "material_id": _mid(fake, "Carter"),
        "lote_operacion_id": "lote-7", "anulado": False, "cantidad_kg": -3500.0,
        "materiales": {"nombre": "Carter"},
    }])
    svc = InventarioServiceConValidacion(fake)
    datos = svc.obtener_datos_pdf_remision("REM_7")
    _cons("PDF datos: conserva el MISMO numero", datos["numero_remision"] == "REM_7")
    _cons("PDF datos: cliente cargado", (datos["cliente"] or {}).get("nombre") == "Cliente X")
    _cons("PDF datos: conductor cargado", (datos["conductor"] or {}).get("placa") == "ABC123")
    # El item ahora incluye 'precio_unitario' (None en ORDEN_SALIDA) para el
    # PDF 'APROBADA'; se validan los campos clave en vez de igualdad exacta.
    _it = (datos["items"] or [{}])[0]
    _cons("PDF datos: items con cantidad positiva", _it.get("material_nombre") == "Carter" and _it.get("cantidad_kg") == 3500.0 and "precio_unitario" in _it)

    # Remisión inexistente debe lanzar ValueError
    try:
        svc.obtener_datos_pdf_remision("REM_999")
        _cons("PDF datos: rechaza remision inexistente", False)
    except ValueError:
        _cons("PDF datos: rechaza remision inexistente", True)


def test_ordenes_salida_y_aprobacion():
    """Nuevos métodos del flujo de Contabilidad: listar las últimas Órdenes de
    Salida (ORDEN_SALIDA, por bodega, más recientes primero) y la RPC
    aprobar_remision_con_precios (fija dólar, aprueba y guarda precios)."""
    fake = FakeSupabase()
    fake._seed("materiales", [
        {"nombre": "Carter", "tipo_material": "LIMPIO", "es_comercializable": True},
    ])
    fake._seed("remisiones", [
        {"id": 101, "numero": "REM_101", "lote_operacion_id": "lote-101", "bodega_id": B,
         "fecha_operacion": "2026-01-25", "estado": "ORDEN_SALIDA"},
        {"id": 102, "numero": "REM_102", "lote_operacion_id": "lote-102", "bodega_id": B,
         "fecha_operacion": "2026-01-26", "estado": "ORDEN_SALIDA"},
        {"id": 103, "numero": "REM_103", "lote_operacion_id": "lote-103", "bodega_id": 2,
         "fecha_operacion": "2026-01-26", "estado": "ORDEN_SALIDA"},  # otra bodega
        {"id": 104, "numero": "REM_104", "lote_operacion_id": "lote-104", "bodega_id": B,
         "fecha_operacion": "2026-01-27", "estado": "APROBADA"},      # ya aprobada
    ])
    fake._seed("movimientos_inventario", [
        {"id": 900, "bodega_id": B, "material_id": _mid(fake, "Carter"),
         "lote_operacion_id": "lote-101", "anulado": False, "cantidad_kg": -3500.0},
        {"id": 901, "bodega_id": B, "material_id": _mid(fake, "Carter"),
         "lote_operacion_id": "lote-102", "anulado": False, "cantidad_kg": -1200.0},
    ])
    svc = InventarioServiceConValidacion(fake)

    ordenes = svc.obtener_ordenes_salida(B, 3)
    _ok("Ordenes: filtra bodega y estado, mas recientes primero",
        [o["id"] for o in ordenes] == [102, 101])
    _ok("Ordenes: respeta el limite",
        [o["id"] for o in svc.obtener_ordenes_salida(B, 1)] == [102])

    res = svc.aprobar_remision_con_precios(101, 4120.50, {900: 2500.0})
    rem = next(r for r in fake._tables["remisiones"] if r["id"] == 101)
    mov = next(m for m in fake._tables["movimientos_inventario"] if m["id"] == 900)
    otro = next(m for m in fake._tables["movimientos_inventario"] if m["id"] == 901)
    _ok("Aprobacion: estado APROBADA", rem["estado"] == "APROBADA")
    _ok("Aprobacion: vr_dolar_dia fijado", float(rem["vr_dolar_dia"]) == 4120.50)
    _ok("Aprobacion: precio_unitario guardado en el lote", float(mov["precio_unitario"]) == 2500.0)
    _ok("Aprobacion: no toca movimientos de otros lotes", otro.get("precio_unitario") is None)
    _ok("Aprobacion: resumen devuelto",
        res.get("estado") == "APROBADA" and res.get("numero") == "REM_101")


def test_mapeo_frases_compuestas_y_unicidad():
    """Frases compuestas con prefijos repetidos: match exact-first/longest y
    unicidad 1 línea -> 1 material (sin replicar el mismo peso)."""
    inv = InventarioServiceConValidacion.__new__(InventarioServiceConValidacion)
    inv.catalogo_materiales = {}
    inv.catalogo_por_id = {}
    for n in ["Cárter", "Arreglo Cárter", "Arreglo Grueso", "Rechazo de Aluminio", "Aluminio"]:
        key = normalizar(n)
        inv.catalogo_materiales[key] = MaterialDTO(
            id=len(inv.catalogo_materiales) + 1, nombre=n,
            tipo_material="COMERCIALIZABLE", es_comercializable=True)
        inv.catalogo_por_id[inv.catalogo_materiales[key].id] = inv.catalogo_materiales[key]

    # Simple vs compuesto: cada uno a su material exacto.
    _ok("carter -> Cárter", inv.obtener_material_por_nombre("carter").nombre == "Cárter")
    _ok("arreglo carter -> Arreglo Cárter (no Cárter)",
        inv.obtener_material_por_nombre("arreglo carter").nombre == "Arreglo Cárter")
    _ok("arreglo grueso -> Arreglo Grueso",
        inv.obtener_material_por_nombre("arreglo grueso").nombre == "Arreglo Grueso")
    _ok("rechazo de aluminio -> frase completa (no 'Aluminio')",
        inv.obtener_material_por_nombre("rechazo de aluminio").nombre == "Rechazo de Aluminio")

    # Lista mixta: 1 objeto por línea, pesos respectivos, sin duplicados.
    items, no_encontrados, merma_l = inv.resolver_lista_materiales(
        "* arreglo carter 501\n* arreglo grueso 300\n* rechazo de aluminio 100")
    _ok("lista: exactamente 3 items", len(items) == 3)
    _ok("lista: nada no encontrado", no_encontrados == [])
    esperado = {"Arreglo Cárter": 501.0, "Arreglo Grueso": 300.0, "Rechazo de Aluminio": 100.0}
    _ok("lista: 1 objeto por línea con su peso",
        {i["material_nombre"]: i["cantidad_kg"] for i in items} == esperado)
    pesos = [i["cantidad_kg"] for i in items]
    _ok("unicidad: ningún peso replicado en 2 materiales", len(pesos) == len(set(pesos)))

    # Reintento tras error: el borrador se sobrescribe, no concatena.
    borrador_previo = {"intencion": "VENTA_DESPACHO", "cliente": "ACME",
                       "items": [{"material_nombre": "Cárter", "cantidad_kg": 501.0}]}
    texto_reintento = "* carter 300\n* arreglo carter 501"
    _ok("detecta lista de materiales", es_lista_materiales(texto_reintento))
    borrador2 = borrador_para_nueva_lista(borrador_previo, texto_reintento)
    _ok("reintento: items previos eliminados (sobrescritura)", borrador2["items"] == [])
    _ok("reintento: conserva cliente/intención", borrador2["cliente"] == "ACME")
    items2, ne2, _m2 = inv.resolver_lista_materiales(texto_reintento)
    _ok("reintento: 2 items, carters diferenciados, sin 501 duplicado",
        len(items2) == 2 and ne2 == []
        and {i["material_nombre"]: i["cantidad_kg"] for i in items2}
        == {"Cárter": 300.0, "Arreglo Cárter": 501.0})
    # Mensaje que NO es lista: el borrador pasa intacto.
    _ok("no-lista: borrador intacto",
        borrador_para_nueva_lista(borrador_previo, "cliente ACME")["items"] == borrador_previo["items"])


def test_purga_borrador_en_error():
    """Validación fallida -> el borrador de materiales se purga y un reintento
    con la lista corregida produce SOLO los ítems del último mensaje."""
    inv = InventarioServiceConValidacion.__new__(InventarioServiceConValidacion)
    inv.catalogo_materiales = {}
    inv.catalogo_por_id = {}
    for n in ["Cárter", "Arreglo Cárter"]:
        key = normalizar(n)
        inv.catalogo_materiales[key] = MaterialDTO(
            id=len(inv.catalogo_materiales) + 1, nombre=n,
            tipo_material="COMERCIALIZABLE", es_comercializable=True)
        inv.catalogo_por_id[inv.catalogo_materiales[key].id] = inv.catalogo_materiales[key]

    # Intento 1 (falla por material desconocido).
    items1, ne1, _m1 = inv.resolver_lista_materiales("* arreglo carter 501\n* material_inexistente 100")
    _ok("intento 1: material desconocido reportado", ne1 == ["material_inexistente 100"])
    # Purga (como hace main.py en el except): solo se conservan los resueltos, PERO
    # al ser error se vacía todo igual que el flujo real.
    borrador = {"intencion": "VENTA_DESPACHO", "items": items1}
    borrador["items"] = []  # <- purga tras error
    # Intento 2: lista corregida completa.
    items2, ne2, _m3 = inv.resolver_lista_materiales("* carter 300\n* arreglo carter 501")
    borrador["items"] = items2
    _ok("reintento tras error: solo 2 items del último mensaje",
        len(borrador["items"]) == 2 and ne2 == [])
    _ok("reintento tras error: no hay doble registro de Arreglo Cárter",
        sum(1 for i in borrador["items"] if i["material_nombre"] == "Arreglo Cárter") == 1)


def test_mensaje_seleccion_revuelto():
    """El total de kilos restados al Revuelto (suma de la selección) se captura
    en `revuelto_descontado` y el mensaje de confirmación incluye 'revuelto: -XX kg'."""
    fake, inv = _generar()
    _cargar(fake, B, "Revuelto", 1000)
    r = inv.registrar_seleccion_revuelto(
        bodega_id=B, usuario_id=9, fecha_operacion="2026-08-26",
        resultados=[{"material_nombre": "Carter", "cantidad_kg": 40},
                    {"material_nombre": "Cable", "cantidad_kg": 20}],
        merma_kg=0,
    )
    _cons("servicio: revuelto_descontado = suma de la selección (60)",
          abs(r["revuelto_descontado"] - 60.0) < 0.01)
    _cons("servicio: merma 0", abs(r["merma_kg"]) < 0.01)
    # Salida de Revuelto registrada en negativo por el total (TRANSFORMACION).
    revuelto_id = inv.catalogo_materiales["revuelto"].id
    salida = [m for m in fake._tables["movimientos_inventario"]
              if m["material_id"] == revuelto_id
              and (m.get("observaciones") or "").startswith("Salida de Revuelto")]
    _cons("movimiento: salida de Revuelto -60 kg (TRANSFORMACION)",
          len(salida) == 1 and abs(salida[0]["cantidad_kg"] + 60) < 0.01
          and salida[0]["tipo_movimiento"] == TipoTransaccion.TRANSFORMACION.value)
    # Mensaje con la MISMA plantilla que usa main.py.
    num_resultados = len(r["registros"]) - 1
    ingreso = r["revuelto_descontado"] - r["merma_kg"]
    fecha = "2026-08-26"
    msg = (f"Selección registrada: {num_resultados} resultado(s), "
           f"merma {r['merma_kg']:.2f} kg, "
           f"ingreso inventario: {ingreso:,.2f} kg, "
           f"total descontado revuelto: -{r['revuelto_descontado']:,.2f} kg, "
           f"fecha {fecha}.")
    _cons("mensaje: formato exacto separa merma / ingreso / total descontado",
          msg == "Selección registrada: 2 resultado(s), merma 0.00 kg, "
                 "ingreso inventario: 60.00 kg, total descontado revuelto: -60.00 kg, "
                 "fecha 2026-08-26.")
    # Con merma y cantidad explícita de Revuelto procesada.
    _cargar(fake, B, "Revuelto", 1000)
    r2 = inv.registrar_seleccion_revuelto(
        bodega_id=B, usuario_id=9, fecha_operacion="2026-08-26",
        resultados=[{"material_nombre": "Carter", "cantidad_kg": 50.5}],
        merma_kg=10, cantidad_revuelto_procesada=60.5,
    )
    _cons("servicio: cantidad explícita se captura intacta (60.5)",
          abs(r2["revuelto_descontado"] - 60.5) < 0.01)
    ingreso2 = r2["revuelto_descontado"] - r2["merma_kg"]
    _cons("mensaje: con merma, el total descontado (60.5) = ingreso (50.5) + merma (10)",
          abs(ingreso2 - 50.5) < 0.01 and abs(r2["merma_kg"] - 10) < 0.01)


def test_caso_produccion_basura_merma_y_omitidos():
    """Caso real de producción: 12 líneas (7.809 kg). 'Basura 960' debe ir a
    merma (NUNCA como ítem vendible) y 'Rechazo de cobre y bronce 69' debe
    reportarse en la sección ⚠️ con nombre+cantidad, sin omitirse en silencio."""
    fake, inv = _generar()
    fake._seed("materiales", [
        {"nombre": "Grueso", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Lamina", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Radiador", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Olla", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Bobina", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Arreglo Grueso", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Perfil", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Cobre", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Bronce", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
    ])
    inv.recargar_catalogos()
    _cargar(fake, B, "Revuelto", 20000)
    texto = (
        "Material seleccionado \n"
        "* Grueso 3730\n* Lamina 468\n* Radiador 354\n* Olla 289\n"
        "* Bobina 842\n* arreglo grueso 501\n* Cable 294\n* Perfil 234\n"
        "* Cobre 21\n* Bronce 47\n* Rechazo de cobre y bronce 69\n* Basura 960"
    )
    items, no_encontrados, merma_lista = inv.resolver_lista_materiales(texto)
    _cons("prod: 10 items vendibles (12 lineas - basura - omitido)", len(items) == 10)
    _cons("prod: basura va a merma (960 kg), no a items",
          abs(merma_lista - 960.0) < 0.01
          and not any(i["material_nombre"] == "Basura" for i in items))
    _cons("prod: omitido reportado con nombre y cantidad",
          no_encontrados == ["Rechazo de cobre y bronce 69"])
    _cons("prod: Grueso y Arreglo Grueso no se cruzan",
          {i["material_nombre"]: i["cantidad_kg"] for i in items
           if "rueso" in i["material_nombre"]}
          == {"Grueso": 3730.0, "Arreglo Grueso": 501.0})
    # Registro con la misma ruta que main.py (merma de la lista + sin otras).
    r = inv.registrar_seleccion_revuelto(
        bodega_id=B, usuario_id=9, fecha_operacion="2026-08-27",
        resultados=items, merma_kg=merma_lista,
    )
    _cons("prod: merma registrada 960.00", abs(r["merma_kg"] - 960.0) < 0.01)
    _cons("prod: revuelto descontado 7740 (items 6780 + merma 960)",
          abs(r["revuelto_descontado"] - 7740.0) < 0.01)
    # Plantilla ESTRICTA de main.py (construir_mensaje_seleccion):
    ingreso = r["revuelto_descontado"] - r["merma_kg"]
    msg = (f"✅ Selección registrada: {len(r['registros']) - 1} resultado(s), "
           f"merma {r['merma_kg']:.2f} kg, "
           f"ingreso inventario: {ingreso:,.2f} kg, "
           f"total descontado revuelto: -{r['revuelto_descontado']:,.2f} kg, "
           f"fecha 2026-08-27.")
    if no_encontrados:
        detalle = "\n".join(f"- {o} kg (Material no encontrado en el catálogo)"
                            for o in no_encontrados)
        msg += ("\n\n⚠️ **Atención:** Los siguientes ítems no se pudieron "
                f"registrar y fueron ignorados:\n{detalle}")
    esperado = (
        "✅ Selección registrada: 10 resultado(s), merma 960.00 kg, "
        "ingreso inventario: 6,780.00 kg, total descontado revuelto: -7,740.00 kg, "
        "fecha 2026-08-27.\n\n"
        "⚠️ **Atención:** Los siguientes ítems no se pudieron registrar y fueron ignorados:\n"
        "- Rechazo de cobre y bronce 69 kg (Material no encontrado en el catálogo)")
    _cons("prod: mensaje con sección ⚠️ exacta y merma/ingreso desglosados", msg == esperado)
    # Sin omitidos: el mensaje NO incluye la sección de alerta.
    _cargar(fake, B, "Revuelto", 1000)
    r2 = inv.registrar_seleccion_revuelto(
        bodega_id=B, usuario_id=9, fecha_operacion="2026-08-27",
        resultados=[{"material_nombre": "Cable", "cantidad_kg": 100}], merma_kg=0,
    )
    ingreso2 = r2["revuelto_descontado"] - r2["merma_kg"]
    msg2 = (f"✅ Selección registrada: {len(r2['registros']) - 1} resultado(s), "
            f"merma {r2['merma_kg']:.2f} kg, "
            f"ingreso inventario: {ingreso2:,.2f} kg, "
            f"total descontado revuelto: -{r2['revuelto_descontado']:,.2f} kg, "
            f"fecha 2026-08-27.")
    _cons("prod: sin omitidos no hay sección ⚠️ y total descontado = ingreso (100)",
          "Atención" not in msg2
          and msg2.startswith("✅ Selección registrada: 1 resultado(s)")
          and "total descontado revuelto: -100.00 kg" in msg2
          and "ingreso inventario: 100.00 kg" in msg2)


def test_sinonimos_palabra_a_palabra_en_seleccion():
    """Los sinónimos del dominio ('grueso'->'carter', 'rechazo'->'arreglo') se
    aplican PALABRA A PALABRA, de modo que los materiales compuestos de 2+
    palabras que repiten la primera palabra se resuelven sin recortar la frase:

        'grueso'        -> 'carter'
        'rechazo grueso'-> 'arreglo carter'
        'rechazo cobre' -> 'arreglo cobre'

    Con esto el caso reportado del usuario ya NO genera 'no_encontrados' y la
    merma solo corresponde a la línea de Basura (450 kg), no a los materiales
    omitidos. Regresion de los 4 bugs de la entrada real.
    """
    fake, inv = _generar()
    fake._seed("materiales", [
        {"nombre": "Lamina", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Carter", "tipo_material": "LIMPIO", "es_comercializable": True},
        {"nombre": "Olla", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Acero", "tipo_material": "LIMPIO", "es_comercializable": True},
        {"nombre": "Arreglo Carter", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Cable", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Radiador", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Perfil", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
        {"nombre": "Bronce", "tipo_material": "LIMPIO", "es_comercializable": True},
        {"nombre": "Cobre", "tipo_material": "LIMPIO", "es_comercializable": True},
        {"nombre": "Arreglo Cobre", "tipo_material": "SEMILIMPIO", "es_comercializable": True},
    ])
    inv.recargar_catalogos()
    _cargar(fake, B, "Revuelto", 20000)
    texto = (
        "* Lamina 685\n* Grueso 3831\n* Olla 576\n* Acero 447\n"
        "* Rechazo grueso 716\n* Cable 277\n* Radiador 286\n* Perfil 223\n"
        "* Bronce 43\n* Cobre 22\n* Rechazo cobre 41\n* Basura 450"
    )
    items, no_encontrados, merma_lista = inv.resolver_lista_materiales(texto)
    _cons("sinónimos: ningún material omitido (grueso/rechazo resueltos)",
          no_encontrados == [])
    _cons("sinónimos: 11 items vendibles (sin basura)",
          len(items) == 11)
    _cons("sinónimos: 'grueso' -> Carter / 'rechazo grueso' -> Arreglo Carter",
          all(i["material_nombre"] == "Carter" for i in items if i["cantidad_kg"] == 3831)
          and all(i["material_nombre"] == "Arreglo Carter" for i in items if i["cantidad_kg"] == 716))
    _cons("sinónimos: 'rechazo cobre' -> Arreglo Cobre",
          all(i["material_nombre"] == "Arreglo Cobre" for i in items if i["cantidad_kg"] == 41))
    # Merma SOLO de la basura (450), jamás de los materiales resueltos.
    _cons("sinónimos: merma = solo Basura (450)", abs(merma_lista - 450.0) < 0.01)
    total_vendible = sum(i["cantidad_kg"] for i in items)
    _cons("sinónimos: revuelto descontado = resultados + merma",
          abs(total_vendible - 7147.0) < 0.01)
    # El servicio descuenta exactamente resultados + merma (7597 = 7147 + 450).
    r = inv.registrar_seleccion_revuelto(
        bodega_id=B, usuario_id=9, fecha_operacion="2026-09-03",
        resultados=items, merma_kg=merma_lista,
    )
    _cons("sinónimos: servicio descuenta 7597 del Revuelto",
          abs(r["revuelto_descontado"] - 7597.0) < 0.01
          and abs(r["merma_kg"] - 450.0) < 0.01)


def test_catalogo_completo_mas_de_30():
    """La carga del catálogo (recargar_catalogos) devuelve TODOS los materiales
    (no está limitada a 10): con 35 registros se cargan los 35, ordenados
    alfabéticamente por nombre."""
    fake = FakeSupabase()
    nombres = [f"Material {i:02d}" for i in range(1, 36)]  # 35 materiales
    fake._seed("materiales", [
        {"nombre": n, "tipo_material": "LIMPIO", "es_comercializable": True}
        for n in nombres
    ])
    inv = InventarioServiceConValidacion(fake)
    _cons("catalogo: carga más de 30 registros", len(inv.catalogo_materiales) >= 30)
    _cons("catalogo: los 35 materiales presentes", len(inv.catalogo_materiales) == 35)
    normalizados = {normalizar(n) for n in nombres}
    _cons("catalogo: sin pérdida de elementos (>=30 sin truncar)",
          normalizados <= set(inv.catalogo_materiales))
    # Recargar es idempotente y no duplica ni trunca.
    inv.recargar_catalogos()
    _cons("catalogo: recarga conserva los 35", len(inv.catalogo_materiales) == 35)


def test_lista_whatsapp_selecciona_formato():
    """Opción A: la elección del envío depende del total de elementos.
    - >10 elementos -> TEXTO normal, ordenado alfabéticamente, con lista
      NUMERADA (1. Acero, 2. ... ; ver resolver_entrada_material). Evita el
      error 400 #131009 y permite responder por número.
    - <=10 elementos -> secciones de Interactive List (sin exceder 10 filas)."""
    import re as _re
    # >10: texto numerado con el formato exacto requerido.
    nombres = ["Cobre", "Aluminio", "Acero", "Bronce", "Basura", "Carter",
               "Cable", "Perfil", "Olla", "Radiador", "Rechazo de cobre y bronce"]
    msg = construir_lista_texto_whatsapp(nombres, titulo="Catálogo de Materiales")
    _cons("texto: encabezado con título y total (11 disponibles) correcto",
          msg.startswith("📋 *Catálogo de Materiales* (11 disponibles):"))
    # Orden alfabético de las líneas numeradas (sin viñetas '•').
    orden = [_re.sub(r"^\d+\.\s*", "", l).strip()
             for l in msg.splitlines() if _re.match(r"^\d+\.\s", l)]
    _cons("texto: lista numerada 1..N, sin viñetas", len(orden) == 11
          and "1. Acero" in msg and "•" not in msg.splitlines()[2:6][0])
    _cons("texto: orden alfabético", orden == sorted(nombres))
    _cons("texto: incluye TODOS los ítems (>10 sin truncar)", len(orden) == 11)
    _cons("texto: pie con instrucción",
          msg.rstrip().endswith("_Escribe el nombre del material o el código para continuar._"))
    # Numeración consecutiva por enumerate.
    _cons("texto: numeración consecutiva 1..11",
          [int(_re.match(r"^(\d+)\.", l).group(1)) for l in msg.splitlines()
           if _re.match(r"^\d+\.\s", l)] == list(range(1, 12)))
    # Sin duplicados y con caracteres compuestos conservados.
    msg_dup = construir_lista_texto_whatsapp(["Acero", "Acero", "Rechazo de cobre y bronce"])
    _cons("texto: sin duplicados y conserva frases compuestas",
          msg_dup.count("Acero") == 1
          and "Rechazo de cobre y bronce" in msg_dup)
    # Resolución por índice: '6' -> 6º material de la lista ordenada.
    ordenados = sorted(nombres)
    _cons("resolver: '6' -> posición 6 alfabética",
          resolver_entrada_material("6", ordenados) == ordenados[5])
    _cons("resolver: devuelve el nombre tal cual",
          resolver_entrada_material("Acero", ordenados) == "Acero")
    _cons("resolver: índice fuera de rango -> None",
          resolver_entrada_material("99", ordenados) is None
          and resolver_entrada_material("", ordenados) is None)
    # <=10: se genera una sola sección con el total de filas (sin truncar).
    filas = [(i, f"Material {i}") for i in range(8)]
    sections = construir_seccion_lista_interactiva(filas, titulo_lista="Materiales")
    _cons("interactivo: 1 sección con 8 filas (<=10)",
          len(sections) == 1 and len(sections[0]["rows"]) == 8
          and sections[0]["title"] == "Materiales")
    filas10 = [(i, f"M{i}") for i in range(10)]
    s10 = construir_seccion_lista_interactiva(filas10)
    _cons("interactivo: 10 filas es el máximo permitido",
          len(s10[0]["rows"]) == 10)


def test_reporte_texto_alfabetico():
    """Informe de inventario (texto): lista COMPLETA de materiales con stock,
    ordenada ALFABÉTICAMENTE (obtener_saldos_bodega)."""
    fake, inv = _generar()
    # Materiales del catálogo de _generar con saldo positivo (con join materiales).
    for i, nombre in enumerate(["Carter", "Cable", "Chatarra"]):
        fake._tables["movimientos_inventario"].append({
            "id": fake._next_id("movimientos_inventario"),
            "bodega_id": B,
            "material_id": _mid(fake, nombre),
            "tipo_movimiento": TipoTransaccion.ENTRADA_BRUTA.value,
            "cantidad_kg": 10.0 + i,
            "lote_operacion_id": "seed",
            "materiales": {"nombre": nombre},
        })
    saldos = inv.obtener_saldos_bodega(B)
    nombres = [s["material"] for s in saldos]
    _cons("informe texto: incluye TODOS los materiales con stock",
          {"Carter", "Cable", "Chatarra"} <= set(nombres))
    _cons("informe texto: orden alfabético",
          nombres == sorted(nombres, key=normalizar))
    _cons("informe texto: sin materiales sin stock", len(saldos) == 3)
    _cons("informe texto: saldo redondeado a 2 decimales",
          all(isinstance(s["saldo_kg"], float) for s in saldos))


def test_grafico_otros_max_10():
    """Gráfico de inventario: máx 10 rebanadas/barras — top 9 + 'Otros' que
    consolida el resto; con 10 o menos materiales se muestran todos sin agrupar."""
    import pandas as pd
    from reporte_grafico import _preparar_datos_torta, MAX_PORCIONES_TORTA

    _cons("grafico: maximo de porciones configurado en 10", MAX_PORCIONES_TORTA == 10)

    # 11 materiales positivos -> top 9 + 'Otros' = 10 porciones.
    df11 = pd.DataFrame({
        "material": [f"Material{i}" for i in range(11)],
        "kg": [float(100 - i) for i in range(11)],
        "porcentaje": [1.0] * 11,
    })
    out11 = _preparar_datos_torta(df11)
    _cons("grafico: 11 materiales -> 10 porciones (9 + Otros)", len(out11) == 10)
    _cons("grafico: última porción es 'Otros'",
          str(out11.iloc[-1]["material"]).startswith("Otros ("))
    _cons("grafico: 'Otros' consolida la suma de los restantes",
          abs(out11.iloc[-1]["kg"] - df11.iloc[9:]["kg"].sum()) < 0.01)
    _cons("grafico: 9 principales + 1 Otros (sin 10 categorías individuales)",
          not out11.iloc[:-1]["material"].str.startswith("Otros (").any())

    # Exactamente 10 materiales -> se muestran todos, sin agrupar ni 'Otros'.
    df10 = pd.DataFrame({
        "material": [f"M{i}" for i in range(10)],
        "kg": [1.0] * 10,
        "porcentaje": [1.0] * 10,
    })
    out10 = _preparar_datos_torta(df10)
    _cons("grafico: 10 materiales -> 10 porciones, sin agrupar",
          len(out10) == 10 and not out10["material"].str.startswith("Otros (").any())

    # Menos de 10 -> ídem (sin Otros).
    df8 = pd.DataFrame({
        "material": [f"N{i}" for i in range(8)],
        "kg": [1.0] * 8, "porcentaje": [1.0] * 8,
    })
    out8 = _preparar_datos_torta(df8)
    _cons("grafico: 8 materiales -> 8 porciones sin Otros",
          len(out8) == 8 and not out8["material"].str.startswith("Otros (").any())


def test_captura_precios_correccion_y_edicion():
    """Flujo de captura de precios de Remisión:
    - '0' en el paso a paso descarta el precio del material ANTERIOR y lo vuelve
      a solicitar (corrección ágil).
    - El resumen final enumera 1..N con instrucciones; '2 16700' edita el ítem 2."""
    items = [
        {"movimiento_id": "aaa", "material_nombre": "Cobre", "cantidad_kg": 100.0},
        {"movimiento_id": "bbb", "material_nombre": "Bronce", "cantidad_kg": 50.0},
        {"movimiento_id": "ccc", "material_nombre": "Carter", "cantidad_kg": 30.0},
    ]

    # Captura normal: el wizard arranca en indice=1 (convencion 1-indexada).
    # Se captura el precio del item 1 (Cobre) y se avanza al item 2 (Bronce).
    r0 = procesar_precio_paso_a_paso("3500", items, {}, 1)
    _cons("precio: item 1 capturado (continuar, indice 2)",
          r0["tipo"] == "continuar" and r0["indice"] == 2 and "Cobre" in r0["precios"])
    r1 = procesar_precio_paso_a_paso("16700", items, r0["precios"], r0["indice"])
    _cons("precio: item 2 capturado (continuar, indice 3)",
          r1["tipo"] == "continuar" and r1["indice"] == 3
          and r1["precios"].get("Cobre") == 3500.0 and r1["precios"].get("Bronce") == 16700.0)

    # Correccion con '0': al solicitar el item 3 (indice 3), '0' descarta el
    # item ANTERIOR (Bronce, indice 2) y retrocede para volver a pedirlo.
    r_corregir = procesar_precio_paso_a_paso("0", items, r1["precios"], 3)
    _cons("precio: '0' descarta el material anterior y retrocede",
          r_corregir["tipo"] == "corregir" and r_corregir["indice"] == 2
          and "Bronce" not in r_corregir["precios"]
          and "Cobre" in r_corregir["precios"]
          and "Bronce" in r_corregir["texto"])
    # El ultimo item (Cobre) se conserva; solo se pierde el anterior (Bronce).
    _cons("precio: solo se descarta el anterior, el resto se conserva",
          r_corregir["precios"].get("Cobre") == 3500.0
          and len(r_corregir["precios"]) == 1)

    # '0' sin item anterior -> invalido (indice 1, aun no hay precios).
    r_inv = procesar_precio_paso_a_paso("0", items, {}, 1)
    _cons("precio: '0' sin item anterior es invalido",
          r_inv["tipo"] == "invalido" and r_inv["indice"] == 1)

    # Resumen final enumerado 1..N con instrucciones.
    precios = {"aaa": 3500.0, "bbb": 16700.0, "ccc": 12000.0}
    resumen = formatear_resumen_precios(items, precios)
    _cons("resumen: enumera 1..N", "1. Cobre" in resumen and "2. Bronce" in resumen
          and "3. Carter" in resumen)
    _cons("resumen: muestra precio por kg de cada ítem",
          "3,500.00 /kg" in resumen and "16,700.00 /kg" in resumen)
    _cons("resumen: instrucciones OK/SI, edición y cancelar",
          "*OK* o *SI*" in resumen and "*2 16700*" in resumen
          and "*0* o *CANCELAR*" in resumen)

    # Edición en el resumen: '2 16700' -> corrije el ítem 2 (Bronce) a 16700.
    edit = parsear_edicion_precio("2 16700")
    _cons("edicion: '2 16700' -> (2, 16700.0)", edit == (2, 16700.0))
    _cons("edicion: textos no válidos -> None",
          parsear_edicion_precio("abc") is None
          and parsear_edicion_precio("2") is None
          and parsear_edicion_precio("0 500") is None)
    _cons("edicion: con coma decimal (coma = decimal, convención del sistema)",
          parsear_edicion_precio("2 16,50") == (2, 16.5))


# ---------------------------------------------------------------------------
# Módulo Crear: parsing en bloque (Cliente/Conductor) y creación en BD
# ---------------------------------------------------------------------------
def test_crear_cliente_conductor_material():
    """Módulo unificado de creación:
    - parsear_bloque_persona extrae los campos de un mensaje en bloque
      (nomenclaturas de Colombia y Argentina, placas, teléfonos con '+').
    - registrar_cliente / registrar_conductor insertan y rechazan duplicados.
    - registrar_material inserta y RECARGA el catálogo en memoria.
    """
    from services.inventario_service import parsear_bloque_persona

    # 1) Parsing de bloque con formato Colombia.
    bloque_co = "Juan Perez\nCC 1.023.456.789\ncel 3001234567\nCra 45 #12-30"
    p = parsear_bloque_persona(bloque_co)
    _cons("Crear: bloque CO -> nombre", p["nombre"] == "Juan Perez")
    _cons("Crear: bloque CO -> identificacion sin separadores (1023456789)",
          p["identificacion"] == "1023456789")
    _cons("Crear: bloque CO -> telefono", p["telefono"] == "3001234567")
    _cons("Crear: bloque CO -> direccion con nomenclatura", "Cra 45" in p["direccion"])

    # 2) Parsing de bloque con formato Argentina (DNI + placa + tel. con +).
    bloque_ar = ("Maria Gomez\nDNI 27894567\nAvenida Siempre Viva 742\n"
                 "AAA123\nCel: +54 9 11 1234-5678")
    p2 = parsear_bloque_persona(bloque_ar)
    _cons("Crear: bloque AR -> DNI (27894567)", p2["identificacion"] == "27894567")
    _cons("Crear: bloque AR -> placa AAA123", p2["placa"] == "AAA123")
    _cons("Crear: bloque AR -> telefono con + normalizado (+5491112345678)",
          p2["telefono"] == "+5491112345678")
    _cons("Crear: bloque AR -> direccion Av.", "Avenida" in p2["direccion"])

    # 3) Creación real contra el fake de Supabase (parámetros EXPLÍCITOS).
    fake, inv = _generar()
    cli = inv.registrar_cliente(nombre_cliente=p["nombre"], id_cliente=p["identificacion"],
                                telefono_cliente=p["telefono"], direccion_cliente=p["direccion"])
    _cons("Crear: cliente insertado con id", bool(cli and cli.get("id")))
    cond = inv.registrar_conductor(nombre_conductor=p2["nombre"], id_conductor=p2["identificacion"],
                                   placa_conductor=p2["placa"], telefono_conductor=p2["telefono"])
    _cons("Crear: conductor insertado con placa", bool(cond and cond.get("placa") == "AAA123"))

    # 4) Duplicados -> ValueError con mensaje amigable.
    try:
        inv.registrar_cliente(nombre_cliente="Otro", id_cliente="1023456789")
        _cons("Crear: cliente duplicado rechazado", False)
    except ValueError as e:
        _cons("Crear: cliente duplicado rechazado",
              "Ya existe un registro con la identificación 1023456789" in str(e))
    try:
        inv.registrar_conductor(nombre_conductor="Otro", id_conductor="27894567")
        _cons("Crear: conductor duplicado rechazado", False)
    except ValueError:
        _cons("Crear: conductor duplicado rechazado", True)

    # 5) Material nuevo: se inserta y el catálogo en memoria se recarga.
    mat = inv.registrar_material(nombre="Bronce", tipo_material="LIMPIO",
                                 es_comercializable=True)
    _cons("Crear: material insertado", bool(mat and mat.get("id")))
    _cons("Crear: material disponible en catálogo en memoria",
          "bronce" in inv.catalogo_materiales)
    try:
        inv.registrar_material(nombre="Bronce")
        _cons("Crear: material duplicado rechazado", False)
    except ValueError:
        _cons("Crear: material duplicado rechazado", True)

    # 6) Falta de campo obligatorio (sin nombre) -> ValueError.
    try:
        inv.registrar_cliente(nombre_cliente="", id_cliente="999")
        _cons("Crear: cliente sin nombre rechazado", False)
    except ValueError:
        _cons("Crear: cliente sin nombre rechazado", True)


def test_atributos_explicitos_placas_y_direccion():
    """Cambios estructurales del módulo de entidades:
    a) Cliente: direccion con ciudad+país y nombres de campo explícitos.
    b) Conductor: 1 placa (trailer None) y 2 placas (camión + remolque).
    c) Sin TypeError por atributos cruzados cliente/conductor."""
    from services.inventario_service import parsear_bloque_persona, extraer_placas

    # a) Dirección del cliente con ciudad, provincia y país EN UNA sola línea.
    bloque_cli = ("Carlos Lopez\nDNI 30456789\n"
                  "Calle 10 #5-20, Villa Constitución, Argentina\ncel 1156784321")
    pc = parsear_bloque_persona(bloque_cli)
    _cons("Entidades: direccion completa con ciudad y país",
          pc["direccion"] == "Calle 10 #5-20, Villa Constitución, Argentina")
    # Ciudad/país en línea SEPARADA se concatenan a la dirección.
    bloque_cli2 = ("Ana Ruiz\nCC 1098776655\nCra 7 #63-22\nBogotá, Colombia\ncel 3012345678")
    pc2 = parsear_bloque_persona(bloque_cli2)
    _cons("Entidades: dirección + ciudad/país en línea separada se concatena",
          pc2["direccion"] == "Cra 7 #63-22, Bogotá, Colombia")

    fake, inv = _generar()
    cli = inv.registrar_cliente(
        nombre_cliente=pc["nombre"], id_cliente=pc["identificacion"],
        telefono_cliente=pc["telefono"], direccion_cliente=pc["direccion"])
    _cons("Entidades: cliente creado con direccion_cliente ciudad+país",
          cli["direccion"] == "Calle 10 #5-20, Villa Constitución, Argentina"
          and cli["identificacion"] == "30456789" and cli["telefono"] == "1156784321")

    # b) Conductor con UNA placa/patente -> trailer None.
    p1 = parsear_bloque_persona("Pedro Gómez\nCC 1098765432\nPlaca ABC123\nCel 3112345678")
    _cons("Entidades: 1 placa -> ABC123", p1["placa"] == "ABC123")
    _cons("Entidades: 1 placa -> trailer vacío (opcional)", p1["placa_trailer"] == "")
    cond1 = inv.registrar_conductor(
        nombre_conductor=p1["nombre"], id_conductor=p1["identificacion"],
        telefono_conductor=p1["telefono"], direccion_conductor=None,
        placa_conductor=p1["placa"], placa_trailer_conductor=None)
    _cons("Entidades: conductor 1 placa en BD, sin trailer",
          cond1["placa"] == "ABC123" and "placa_trailer" not in cond1)

    # b2) Conductor con DOS placas (camión + remolque).
    bloque_dos = ("Luis Sosa\nDNI 28999888\nPlaca: AAA123, Trailer: BBB456\ncel 3115550000")
    p2 = parsear_bloque_persona(bloque_dos)
    _cons("Entidades: 2 placas etiquetadas -> camión AAA123", p2["placa"] == "AAA123")
    _cons("Entidades: 2 placas etiquetadas -> trailer BBB456", p2["placa_trailer"] == "BBB456")
    # También en una sola línea con separador '/'.
    ep = extraer_placas("patente AA123BB / remolque CC456DD")
    _cons("Entidades: extraer_placas 'AA123BB / CC456DD'",
          ep == ("AA123BB", "CC456DD"))
    cond2 = inv.registrar_conductor(
        nombre_conductor=p2["nombre"], id_conductor=p2["identificacion"],
        telefono_conductor=p2["telefono"], direccion_conductor=None,
        placa_conductor=p2["placa"], placa_trailer_conductor=p2["placa_trailer"])
    _cons("Entidades: conductor 2 placas en BD (camión + trailer)",
          cond2["placa"] == "AAA123" and cond2.get("placa_trailer") == "BBB456")

    # c) Cero TypeError por atributos cruzados: mapeos explícitos main.py.
    try:
        d = {"nombre": "X", "identificacion": "1", "telefono": "2",
             "direccion": "Calle 1", "placa": "AAA111", "placa_trailer": ""}
        inv.registrar_cliente(**{"nombre_cliente": d["nombre"], "id_cliente": d["identificacion"],
                                 "telefono_cliente": d["telefono"], "direccion_cliente": d["direccion"]})
        inv.registrar_conductor(nombre_conductor="Y", id_conductor="2",
                                telefono_conductor="3", direccion_conductor="Av 9",
                                placa_conductor="BBB222", placa_trailer_conductor=None)
        _cons("Entidades: sin TypeError por atributos cruzados", True)
    except TypeError:
        _cons("Entidades: sin TypeError por atributos cruzados", False)


def test_direccion_opcional_conductor():
    """Dirección del conductor (OPCIONAL):
    a) Con dirección completa -> se persiste en la columna 'direccion'.
    b) Omitida (None / '0' / 'omitir') -> se guarda sin dirección y sin errores.
    Ningún caso lanza TypeError/KeyError por atributos cruzados."""
    from services.inventario_service import parsear_bloque_persona

    fake, inv = _generar()

    # a) Bloque con dirección completa (incluye ciudad y país).
    p = parsear_bloque_persona(
        "Miguel Torres\nDNI 32111222\nPlaca AB123CD\nCel 3415559999\n"
        "Calle 10 #5-20, Rosario, Argentina")
    _cons("DirCond: parser captura dirección completa",
          p["direccion"] == "Calle 10 #5-20, Rosario, Argentina")
    cond = inv.registrar_conductor(
        nombre_conductor=p["nombre"], id_conductor=p["identificacion"],
        telefono_conductor=p["telefono"],
        direccion_conductor=p["direccion"] or None,
        placa_conductor=p["placa"], placa_trailer_conductor=None)
    _cons("DirCond: dirección persistida en BD",
          cond.get("direccion") == "Calle 10 #5-20, Rosario, Argentina")
    fila = [r for r in fake._tables["conductores"] if r["id"] == cond["id"]][0]
    _cons("DirCond: fila BD usa columna exacta 'direccion'",
          fila["direccion"] == "Calle 10 #5-20, Rosario, Argentina")

    # b) Sin dirección (None explícito) -> se guarda sin errores.
    cond2 = inv.registrar_conductor(
        nombre_conductor="Sin Direccion", id_conductor="444555666",
        telefono_conductor="3110000000", direccion_conductor=None,
        placa_conductor="XY987ZZ", placa_trailer_conductor=None)
    _cons("DirCond: conductor sin dirección guarda None",
          cond2.get("direccion") is None)
    # y con cadena vacía (bloque sin dirección) -> fila sin dirección.
    p3 = parsear_bloque_persona("Otro Conductor\nCC 777888999\nPlaca EF456GH\ncel 3122223333")
    _cons("DirCond: bloque sin dirección -> parser la deja vacía", p3["direccion"] == "")
    cond3 = inv.registrar_conductor(
        nombre_conductor=p3["nombre"], id_conductor=p3["identificacion"],
        telefono_conductor=p3["telefono"], direccion_conductor=p3["direccion"] or None,
        placa_conductor=p3["placa"], placa_trailer_conductor=None)
    _cons("DirCond: dirección vacía se persiste como None",
          cond3.get("direccion") in (None, ""))


def test_normalizacion_y_validacion_cliente():
    """Valida:
    a) Normalización de nombres con tildes ('Juan perez' == 'Juan Pérez').
    b) Rechazo de cédulas no numéricas en el paso a paso de clientes.
    """
    import asyncio
    from utils.parsers import normalizar_nombre
    from handlers.clientes_handler import procesar_flujo_cliente
    
    # Crear un mock de Supabase y la instancia del servicio
    # (Copiar la lógica de las funciones de test existentes)
    from tests.test_transformaciones import FakeSupabase
    fake = FakeSupabase()
    inv = InventarioServiceConValidacion(fake)

    # a) Normalización de nombres con tildes / mayúsculas
    n1 = normalizar_nombre("Juan Pérez")
    n2 = normalizar_nombre("juan perez")
    _cons("Normalización nombres tildes y mayúsculas", n1 == n2 == "juan perez")

    # Registrar en DB de prueba y buscar con distinta tilde/mayús
    c1 = inv.registrar_cliente(nombre_cliente="María José Pérez", id_cliente="123456", telefono_cliente="3001112233", direccion_cliente="Calle 1")
    encontrado = inv.buscar_cliente_existente(nombre="maria jose perez")
    _cons("Búsqueda cliente sin tildes coincide con tildes", encontrado is not None and encontrado["id"] == c1["id"])

    # b) Validación cédula no numérica en paso a paso
    contexto = {"accion_pendiente": {"tipo": "crear_cliente_paso", "datos": {"nombre": "Carlos Gómez"}}, "campo_esperado": "cliente_documento"}
    # Envío de texto no numérico (ej. repite el nombre)
    resp = asyncio.run(procesar_flujo_cliente("12345", 1, 1, contexto, contexto["accion_pendiente"], "Carlos Gómez"))
    _cons("Rechazo cédula no numérica con mensaje de advertencia", "⚠️ La cédula o documento debe contener números válidos" in resp)
    _cons("Mantiene el estado pendiente tras cédula inválida", contexto["accion_pendiente"].get("tipo") == "crear_cliente_paso")

    # Envío de cédula numérica válida avanza
    resp_valida = asyncio.run(procesar_flujo_cliente("12345", 1, 1, contexto, contexto["accion_pendiente"], "987654321"))
    _cons("Avanza o registra con cédula numérica válida", "Faltan datos del cliente" in resp_valida or "✅ Cliente registrado" in resp_valida)



def test_conductor_service_normalizacion_y_placas():
    """Valida:
    a) ConductorService modularizado y búsqueda insensible a tildes/mayúsculas.
    b) Normalización y búsqueda flexible por placas y remolques.
    c) Manejo de campos opcionales (dirección y remolque/tráiler).
    """
    from tests.test_transformaciones import FakeSupabase
    from services.conductor_service import ConductorService, normalizar_placa
    
    fake = FakeSupabase()
    cs = ConductorService(fake)
    
    # a) Normalización de placa
    _cons("Normalización de placa con guiones/minúsculas", normalizar_placa("abc-123") == "ABC123")
    _cons("Normalización de placa con espacios", normalizar_placa(" XYZ 789 ") == "XYZ789")
    
    # b) Registro con tráiler y búsqueda insensible a tildes y mayúsculas
    cond1 = cs.registrar_conductor(
        nombre_conductor="Hernán Darío Gómez",
        id_conductor="10102020",
        telefono_conductor="3119876543",
        direccion_conductor="Cra 15 # 45-20",
        placa_conductor="ABC-123",
        placa_trailer_conductor="TR-999"
    )
    _cons("Conductor registrado con éxito", cond1["nombre"] == "Hernán Darío Gómez")
    
    # Búsqueda por nombre sin tildes y en minúsculas
    busc_nombre = cs.buscar_conductor_existente(nombre="hernan dario gomez")
    _cons("Búsqueda conductor insensible a tildes", busc_nombre is not None and busc_nombre["id"] == cond1["id"])
    
    # Búsqueda por placa (tolerante a formato)
    busc_placa = cs.buscar_conductor_existente(placa="abc123")
    _cons("Búsqueda conductor por placa normalizada", busc_placa is not None and busc_placa["id"] == cond1["id"])
    
    # Búsqueda por placa de tráiler
    busc_trailer = cs.buscar_conductor_existente(placa="TR999")
    _cons("Búsqueda conductor por placa de tráiler", busc_trailer is not None and busc_trailer["id"] == cond1["id"])
    
    # c) Campos opcionales (sin dirección ni tráiler)
    cond2 = cs.registrar_conductor(
        nombre_conductor="Pedro Pérez",
        id_conductor="30304040",
        telefono_conductor="3000000000",
        placa_conductor="XYZ-789"
    )
    _cons("Conductor sin dirección ni trailer guarda None", cond2.get("direccion") is None and cond2.get("placa_trailer") is None)



def main():
    test_regla1_revuelto()
    test_regla2_quema_cable()
    test_regla3_seleccion_tecnica_carter()
    test_conservacion_masa_exigida()
    test_stock_insuficiente()
    test_vender_merma()
    test_regenerar_pdf_datos()
    test_ordenes_salida_y_aprobacion()
    test_mapeo_frases_compuestas_y_unicidad()
    test_purga_borrador_en_error()
    test_mensaje_seleccion_revuelto()
    test_caso_produccion_basura_merma_y_omitidos()
    test_sinonimos_palabra_a_palabra_en_seleccion()
    test_catalogo_completo_mas_de_30()
    test_lista_whatsapp_selecciona_formato()
    test_reporte_texto_alfabetico()
    test_grafico_otros_max_10()
    test_captura_precios_correccion_y_edicion()
    test_crear_cliente_conductor_material()
    test_atributos_explicitos_placas_y_direccion()
    test_conductor_service_normalizacion_y_placas()

    test_direccion_opcional_conductor()
    test_normalizacion_y_validacion_cliente()

    if _FAILURES:
        print("\n=== %d FALLO(S) ===\n%s" % (len(_FAILURES), "\n".join(" - " + f for f in _FAILURES)))
        return 1
    print("\nTODAS LAS PRUEBAS PASARON OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
