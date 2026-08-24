import os
from dotenv import load_dotenv
from supabase import create_client, Client

# 1. Cargar las variables de entorno desde el archivo .env
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Error: Faltan las llaves SUPABASE_URL o SUPABASE_KEY en el archivo .env")
    exit()

# 2. Conectar con Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def probar_vistas():
    print("🔍 Conectando a Supabase para consultar las vistas...\n")

    # Pruebas 1: Vista Balance de Inventario
    try:
        res_balance = supabase.table("vista_balance_inventario").select("*").execute()
        print("✅ --- PRUEBA VISTA: vista_balance_inventario ---")
        if res_balance.data:
            for item in res_balance.data:
                print(f"📦 Bodega: {item.get('bodega')} | Material: {item.get('material')} | Stock: {item.get('stock_actual_kg')} kg")
        else:
            print("ℹ️ La vista devolvió 0 registros (no hay movimientos aún).")
    except Exception as e:
        print(f"❌ Error al consultar 'vista_balance_inventario': {e}")

    print("\n------------------------------------------------\n")

    # Prueba 2: Vista Reporte Diario
    try:
        res_diario = supabase.table("vista_reporte_diario").select("*").execute()
        print("✅ --- PRUEBA VISTA: vista_reporte_diario ---")
        if res_diario.data:
            for item in res_diario.data:
                print(f"📅 Fecha: {item.get('fecha_operacion')} | Material: {item.get('material')} | Total: {item.get('total_kg')} kg")
        else:
            print("ℹ️ La vista devolvió 0 registros.")
    except Exception as e:
        print(f"❌ Error al consultar 'vista_reporte_diario': {e}")

if __name__ == "__main__":
    probar_vistas()