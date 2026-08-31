"""Test del helper de mapeo de precios."""
from handlers.remisiones_handler import _construir_precios_items

items = [
    {"movimiento_id": "aaa-001", "material_nombre": "Cobre", "cantidad_kg": 100.0},
    {"movimiento_id": "bbb-002", "material_nombre": "Bronce", "cantidad_kg": 50.0},
    {"movimiento_id": "ccc-003", "material_nombre": "Carter", "cantidad_kg": 30.0},
]

print("=== Caso 1: precios por nombre (wizard normal) ===")
precios = {"Cobre": 16000.0, "Bronce": 12000.0, "Carter": 8000.0}
resultado, sin_precio = _construir_precios_items(items, precios)
print("Resultado:", resultado)
print("Sin precio:", sin_precio)
print()

print("=== Caso 2: precios por movimiento_id (legacy) ===")
precios_legacy = {"aaa-001": 16000.0, "bbb-002": 12000.0, "ccc-003": 8000.0}
resultado, sin_precio = _construir_precios_items(items, precios_legacy)
print("Resultado:", resultado)
print("Sin precio:", sin_precio)
print()

print("=== Caso 3: mixto (algunos por mid, otros por nombre) ===")
precios_mixto = {"Cobre": 16000.0, "bbb-002": 12000.0, "ccc-003": 8000.0}
resultado, sin_precio = _construir_precios_items(items, precios_mixto)
print("Resultado:", resultado)
print("Sin precio:", sin_precio)
print()

print("=== Caso 4: falta Carter ===")
precios_faltante = {"Cobre": 16000.0, "Bronce": 12000.0}
resultado, sin_precio = _construir_precios_items(items, precios_faltante)
print("Resultado:", resultado)
print("Sin precio:", sin_precio)
print()

print("=== Caso 5: items con mid None ===")
items_sin_mid = [
    {"movimiento_id": None, "material_nombre": "Cobre", "cantidad_kg": 10.0},
    {"movimiento_id": "bbb-002", "material_nombre": "Bronce", "cantidad_kg": 5.0},
]
precios = {"Cobre": 100.0, "Bronce": 200.0}
resultado, sin_precio = _construir_precios_items(items_sin_mid, precios)
print("Resultado:", resultado)
print("Sin precio:", sin_precio)
print()

print("=== Calculo de total con caso 1 ===")
total = 0.0
for it in items:
    mid = str(it["movimiento_id"])
    if mid in resultado:
        sub = it["cantidad_kg"] * resultado[mid]
        print("  {0:10s} {1:6.2f} kg x {2:10.2f} = {3:14,.2f}".format(
            it["material_nombre"], it["cantidad_kg"], resultado[mid], sub))
        total += sub
print("TOTAL: {:,.2f}".format(total))
