import os
import requests
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "").strip()


def probar_supabase():
    print("\n--- 1. PROBANDO CONEXIÓN A SUPABASE ---")
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = supabase.table("vista_balance_inventario").select("*").limit(1).execute()
        print("✅ Supabase responde correctamente.")
        print("   Respuesta:", res.data)
    except Exception as e:
        print(f"❌ Error conectando a Supabase: {e}")


def probar_whatsapp_envio(numero_destino: str):
    print("\n--- 2. PROBANDO ENVÍO REAL VÍA META WHATSAPP API ---")
    
    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero_destino,
        "type": "text",
        "text": {"body": "🤖 Mensaje de prueba: Conexión exitosa."}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        res_json = response.json()

        if response.status_code == 200:
            print("✅ Mensaje enviado con éxito a WhatsApp.")
        else:
            print(f"❌ Error al enviar mensaje (HTTP {response.status_code}):")
            print("   Detalles:", res_json)
    except Exception as e:
        print(f"❌ Error de red al conectar con WhatsApp: {e}")


# INICIO DE EJECUCIÓN (Indispensable para que corra el script)
if __name__ == "__main__":
    print("Iniciando pruebas de conexión...")
    probar_supabase()
    
    # Reemplaza con tu número real (ej. 573001234567)
    NUMERO_PRUEBA = "573107540026"  
    probar_whatsapp_envio(NUMERO_PRUEBA)