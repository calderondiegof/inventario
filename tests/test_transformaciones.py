"""Pruebas offline de la transformacion de materiales (4 estados).

Ejecutar sin base de datos real:
    python -m tests.test_transformaciones

Usa un cliente Supabase simulado para validar la conservacion de masa y que
la MERMA (Basura/Tierra) queda como stock vendible.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# El directorio local `supabase/` (migraciones SQL) ENSOMBRECE al paquete pip
# `supabase` cuando el CWD es la raiz del proyecto. Para poder importar el
# servicio offline inyectamos un modulo `supabase` de reemplazo: `Client` solo
# se usa como anotacion de tipo, asi que un `object` es suficiente.
_stub = types.ModuleType("supabase")
_stub.Client = object
sys.modules.setdefault("supabase", _stub)

from services.inventario_service import (
    InventarioServiceConValidacion,
    TipoTransaccion,
    MaterialDTO,
    normalizar,
    es_lista_materiales,
    borrador_para_nueva_lista,
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
    svc.registrar_transformacion_material(
        bodega_id=B, usuario_id=3, fecha_operacion="2026-01-26",
        material_origen_nombre="Cable",
        resultados=[{"material_nombre": "Cable Quemado", "cantidad_kg": 600}],
        merma_kg=400, nombre_proceso="Quema de Cable",
    )
    _cons("Regla2: Cable -1000", abs(_saldo(fake, B, "Cable")) < 0.01)
    _cons("Regla2: Cable Quemado +600", abs(_saldo(fake, B, "Cable Quemado") - 600) < 0.01)
    _cons("Regla2: Basura +400", abs(_saldo(fake, B, "Basura") - 400) < 0.01)
    _cons("Regla2: NO afecta Revuelto", abs(_saldo(fake, B, "Revuelto")) < 0.01)


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
    items, no_encontrados = inv.resolver_lista_materiales(
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
    items2, ne2 = inv.resolver_lista_materiales(texto_reintento)
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
    items1, ne1 = inv.resolver_lista_materiales("* arreglo carter 501\n* material_inexistente 100")
    _ok("intento 1: material desconocido reportado", ne1 == ["material_inexistente"])
    # Purga (como hace main.py en el except): solo se conservan los resueltos, PERO
    # al ser error se vacía todo igual que el flujo real.
    borrador = {"intencion": "VENTA_DESPACHO", "items": items1}
    borrador["items"] = []  # <- purga tras error
    # Intento 2: lista corregida completa.
    items2, ne2 = inv.resolver_lista_materiales("* carter 300\n* arreglo carter 501")
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
    fecha = "2026-08-26"
    msg = (f"Selección registrada: {num_resultados} resultado(s), "
           f"merma {r['merma_kg']:.2f} kg, revuelto: -{r['revuelto_descontado']:g} kg, "
           f"fecha {fecha}.")
    _cons("mensaje: formato exacto con 'revuelto: -60 kg'",
          msg == "Selección registrada: 2 resultado(s), merma 0.00 kg, revuelto: -60 kg, fecha 2026-08-26.")
    # Con merma y cantidad explícita de Revuelto procesada.
    _cargar(fake, B, "Revuelto", 1000)
    r2 = inv.registrar_seleccion_revuelto(
        bodega_id=B, usuario_id=9, fecha_operacion="2026-08-26",
        resultados=[{"material_nombre": "Carter", "cantidad_kg": 50.5}],
        merma_kg=10, cantidad_revuelto_procesada=60.5,
    )
    _cons("servicio: cantidad explícita se captura intacta (60.5)",
          abs(r2["revuelto_descontado"] - 60.5) < 0.01)
    msg2 = f"revuelto: -{r2['revuelto_descontado']:g} kg"
    _cons("mensaje: :g formatea sin ceros ni comas", msg2 == "revuelto: -60.5 kg")


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
    if _FAILURES:
        print("\n=== %d FALLO(S) ===\n%s" % (len(_FAILURES), "\n".join(" - " + f for f in _FAILURES)))
        return 1
    print("\nTODAS LAS PRUEBAS PASARON OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())