import requests

account_sid = 'AC29ecc9883a1ed184962a42fddd2792da'
auth_token = 'bef2db32d8c69c99e3370a0010d57f37'

# Tu URL pública apuntando a la ruta de FastAPI
webhook_url = 'https://pagan-darling-unmixed.ngrok-free.dev/webhook/whatsapp'

# 1. Obtenemos el SID de tu Messaging Service activo
url_get_services = f'https://messaging.twilio.com/v1/Services'
response = requests.get(url_get_services, auth=(account_sid, auth_token)).json()

if response.get('services'):
    service_sid = response['services'][0]['sid']
    print(f"Messaging Service encontrado: {service_sid}")
    
    # 2. Actualizamos la URL del Webhook para mensajes entrantes
    url_update = f'https://messaging.twilio.com/v1/Services/{service_sid}'
    data = {
        'InboundRequestUrl': webhook_url,
        'InboundMethod': 'POST'
    }
    
    res = requests.post(url_update, data=data, auth=(account_sid, auth_token))
    
    if res.status_code == 200:
        print("\n==========================================")
        print("¡ÉXITO! Webhook guardado correctamente.")
        print("==========================================\n")
    else:
        print(f"Error al actualizar: {res.text}")
else:
    print("No se encontró ningún Messaging Service.")