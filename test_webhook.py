import requests

# URL con la ruta correcta
url = "http://127.0.0.1:8000/webhook/whatsapp"

# JSON simulando mensaje de WhatsApp
payload = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "contacts": [{"profile": {"name": "Alexander"}}],
                        "messages": [
                            {
                                "from": "573001234567",
                                "text": {"body": "Arreglamos 100 kg de Cable"}
                            }
                        ]
                    }
                }
            ]
        }
    ]
}

try:
    respuesta = requests.post(url, json=payload)
    print("Estado HTTP:", respuesta.status_code)
    # Cambiamos .json() por .text para evitar el error de parsing
    print("Respuesta de la app:", respuesta.text)
except Exception as e:
    print("Error al conectar:", e)