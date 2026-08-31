"""Test del nuevo servicio PDF de remision."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path('.').resolve()))
from services.pdf_remision_service import (
    PdfRemisionService, RemisionPdf, construir_filas_listado_pdf
)

# 1. Normalizacion
assert PdfRemisionService.normalizar_numero("117") == "REM_117"
assert PdfRemisionService.normalizar_numero("rem_117") == "REM_117"
assert PdfRemisionService.normalizar_numero("REM-117") == "REM_117"
assert PdfRemisionService.normalizar_numero("Remision 117") == "REM_117"
assert PdfRemisionService.normalizar_numero("") == ""
assert PdfRemisionService.normalizar_numero(None) == ""
print("OK 1: normalizar_numero")

# 2. DTO
r = RemisionPdf("REM_117", "https://x.supabase.co/remisiones/REM_117.pdf", "Acme SA", "2026-08-31T10:00:00Z")
assert r.filename == "REM_117.pdf"
assert r.caption.startswith("Remision REM_117")
print("OK 2: DTO")

# 3. Supabase None -> None
svc = PdfRemisionService(supabase=None)
assert svc.obtener_pdf_remision("117") is None
assert svc.listar_remisiones_con_pdf(10) == []
print("OK 3: supabase None")

# 4. Supabase con stub
class StubTable:
    def __init__(self, data=None): self._data = data or []
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def not_(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        class R: data = self._data
        return R()

class StubSB:
    def __init__(self, data): self.t = StubTable(data)
    def table(self, name): return self.t

data = [{
    "numero_remision": "REM_117",
    "pdf_url": "https://x.supabase.co/remisiones/REM_117.pdf",
    "cliente": "Acme SA",
    "fecha_creacion": "2026-08-31T10:00:00Z",
}]
svc = PdfRemisionService(supabase=StubSB(data))
r = svc.obtener_pdf_remision("rem 117")
assert r is not None, "debio encontrar REM_117"
assert r.numero_remision == "REM_117"
assert r.cliente == "Acme SA"
print(f"OK 4: obtener -> {r.numero_remision} {r.cliente}")

# 5. Sin pdf_url
svc = PdfRemisionService(supabase=StubSB([{"numero_remision": "REM_5", "pdf_url": None, "cliente": "X"}]))
assert svc.obtener_pdf_remision("5") is None
print("OK 5: sin pdf_url -> None")

# 6. listar
svc = PdfRemisionService(supabase=StubSB(data * 3))
lst = svc.listar_remisiones_con_pdf(10)
assert len(lst) == 3
print(f"OK 6: listar -> {len(lst)} remisiones")

# 7. construir_filas_listado_pdf
filas = construir_filas_listado_pdf([
    RemisionPdf("REM_117", "https://x/p.pdf", "Acme SA", "2026-08-31"),
    RemisionPdf("REM_116", "https://x/p.pdf", "Bronce SRL", None),
])
assert len(filas) == 2
assert filas[0][0] == "REM_117"
assert filas[0][1] == "REM_117"  # titulo truncado
print("OK 7: construir_filas")

print()
print("TODOS LOS TESTS DEL SERVICIO PDF PASARON")
