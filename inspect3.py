import io, re
s = io.open('services/inventario_service.py', encoding='utf-8').read()
for n in ('normalizar_digitos', 'buscar_cliente_existente', 'registrar_cliente'):
    m = re.search(r'def ' + n + r'.*?(?=\ndef |\nclass |\n[A-Za-z_]+\s*=)', s, re.S)
    print('---', n, '---')
    print(m.group(0) if m else 'NOT FOUND')
