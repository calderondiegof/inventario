#!/usr/bin/env python3
"""Script para probar el webhook manualmente"""

import requests
import json

print("=" * 60)
print("PRUEBA 1: Verificar que el servidor está corriendo")
print("=" * 60)

try:
    response = requests.get("https://pagan-darling-unmixed.ngrok-free.dev/test")
    print(f"✅ Servidor respondiendo: {response.status_code}")
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))
except Exception as e:
    print(f"❌ Error: {e}")
    exit(1)

print("\n" + "=" * 60)
print("PRUEBA 2: Verificar endpoint de debug")
print("=" * 60)

try:
    response = requests.get("https://pagan-darling-unmixed.ngrok-free.dev/debug")
    print(f"✅ Debug endpoint: {response.status_code}")
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))
except Exception as e:
    print(f"❌ Error: {e}")

print("\n" + "=" * 60)
print("PRUEBA 3: Enviar webhook de prueba")
print("=" * 60)

webhook_data = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "123456789",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "1234567890",
                            "phone_number_id": "123456789",
                            "business_account_id": "987654321"
                        },
                        "contacts": [
                            {
                                "profile": {"name": "Usuario de Prueba"},
                                "wa_id": "573001234567"
                            }
                        ],
                        "messages": [
                            {
                                "from": "573001234567",
                                "id": "wamid.test123",
                                "timestamp": "1692100000",
                                "type": "text",
                                "text": {
                                    "body": "Hola, esto es una prueba"
                                }
                            }
                        ]
                    },
                    "field": "messages"
                }
            ]
        }
    ]
}

try:
    response = requests.post(
        "https://pagan-darling-unmixed.ngrok-free.dev/webhook/test",
        json=webhook_data,
        timeout=10
    )
    print(f"✅ Webhook enviado: {response.status_code}")
    print(f"Respuesta: {response.text}")
except Exception as e:
    print(f"❌ Error: {e}")

print("\n" + "=" * 60)
print("REVISA LOS LOGS DEL SERVIDOR EN LA TERMINAL")
print("Deberías ver mensajes que comienzan con 🔍, 📨, 📥, etc.")
print("=" * 60)
