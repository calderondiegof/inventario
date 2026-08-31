"""Script para reemplazar obtener_datos_completos_remision en pdf_remision_service.py"""
import pathlib

DEST = r"c:\Users\Alexander Calderon\agente_inventario\inventario\services\pdf_remision_service.py"

with open(DEST, encoding="utf-8") as fh:
    s = fh.read()

# --- localizar inicio y fin del bloque a reemplazar ---
START = "    def obtener_datos_completos_remision(self, numero: str) -> Dict[str, Any]:"
idx_start = s.find(START)
idx_end = s.find("\n    def ", idx_start + len(START) + 1)
if idx_start < 0 or idx_end < 0:
    print("ERROR: no encontre los limites del metodo")
    raise SystemExit(1)

old_block = s[idx_start:idx_end]

new_block = r"""    def obtener_datos_completos_remision(self, numero: str) -> Dict[str, Any]:
        """Reune todos los datos para regenerar el PDF de una remision existente.

        Arquitectura confirmada (old service):
          - remisiones tiene: numero, lote_operacion_id, cliente_id, conductor_id,
            bodega_id, fecha_operacion, estado, vr_dolar_dia.
          - Los items/materiales y pesos estan en movimientos_inventario,
            unidos a materiales(nombre) por material_id.
          - cliente y conductor se buscan por id en sus tablas.

        El PDF muestra valores POSITIVOS (la venta/despacho se guarda con
        cantidad_kg negativa). El precio_unitario viene de la aprobacion
        (RPC); si no se aprobo aun las columnas financieras se omiten.
        """
        if not self._supabase:
            raise PdfRemisionError("Supabase no disponible")

        num = self.normalizar_numero(numero)
        if not num:
            raise PdfRemisionError(f"Numero invalido: '{numero}'")

        # --- 1) Remision ---
        res = self._supabase.table("remisiones").select("*").eq("numero", num).limit(1).execute()
        filas = getattr(res, "data", None) or []
        if not filas:
            raise PdfRemisionNoEncontrada(f"No existe la remision '{num}'")
        rem = filas[0]

        # --- 2) Cliente ---
        cliente = {}
        if rem.get("cliente_id"):
            c = (self._supabase.table("clientes")
                 .select("*").eq("id", rem["cliente_id"]).limit(1).execute())
            if getattr(c, "data", None):
                cliente = c.data[0] or {}

        # --- 3) Conductor ---
        conductor = {}
        if rem.get("conductor_id"):
            d = (self._supabase.table("conductores")
                 .select("*").eq("id", rem["conductor_id"]).limit(1).execute())
            if getattr(d, "data", None):
                conductor = d.data[0] or {}

        # --- 4) Movimientos con JOIN embebido a materiales(nombre) ---
        #    Mismo patron exacto que obtener_remision() en old service.
        movimientos = (
            self._supabase.table("movimientos_inventario")
            .select("id,material_id,cantidad_kg,precio_unitario,tipo_movimiento,"
                    "observaciones,materiales(nombre)")
            .eq("lote_operacion_id", rem.get("lote_operacion_id"))
            .eq("anulado", False)
            .execute().data
        ) or []

        # --- 5) Mapear al formato que espera generar_remision_pdf_archivo ---
        #    Mismo patron que obtener_datos_pdf_remision() en old service:
        #    cantidad_kg -> abs(), nombre desde materiales(nombre).
        items = []
        for m in movimientos:
            try:
                cantidad_raw = m.get("cantidad_kg") or m.get("cantidad") or m.get("peso_kg") or 0
                cantidad = abs(float(cantidad_raw))
            except (TypeError, ValueError):
                cantidad = 0.0
            if cantidad <= 0:
                continue

            mat_nombre = (
                (m.get("materiales") or {}).get("nombre")
                or (m.get("productos") or {}).get("nombre")
                or m.get("material")
                or m.get("descripcion")
                or "Material"
            )

            items.append({
                "material_nombre": mat_nombre,
                "cantidad_kg": cantidad,
                "precio_unitario": m.get("precio_unitario") or 0.0,
                "observaciones": m.get("observaciones"),
                "tipo_movimiento": m.get("tipo_movimiento"),
            })

        return {
            "numero_remision": rem.get("numero", num),
            "fecha_operacion": rem.get("fecha_operacion"),
            "bodega_id": rem.get("bodega_id"),
            "estado": rem.get("estado"),
            "vr_dolar_dia": rem.get("vr_dolar_dia"),
            "cliente": cliente,
            "conductor": conductor,
            "items": items,
        }
"""

s2 = s[:idx_start] + new_block + s[idx_end:]
with open(DEST, "w", encoding="utf-8") as fh:
    fh.write(s2)
print("OK")
