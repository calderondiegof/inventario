import io

p = "services/inventario_service.py"
s = io.open(p, encoding="utf-8").read()
orig = s

pairs = [
    ('.ilike("nombre", nombre)',
     '.ilike("nombre", normalizar(nombre))'),
    ('.ilike("nombre", (nombre or "").strip())',
     '.ilike("nombre", normalizar(nombre or ""))'),
    ('.ilike("nombre", (nombre or "").strip()))',
     '.ilike("nombre", normalizar(nombre or ""))))'),
]

n = 0
for a, b in pairs:
    c = s.count(a)
    s = s.replace(a, b)
    n += c

io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("reemplazos:", n, "cambio_total:", orig != s)
